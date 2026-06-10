# Phase 2 `view_image` Specification

## Boundary

`view_image` is a model-visible native tool for semantic inspection of one to
four local PNG or JPEG images.

It is not a general file reader, browser, screenshot tool, image cache,
artifact cache, or RenderDoc-specific command. RenderDoc workflows may produce
PNG or JPEG files and then ask `view_image` to inspect them, but runtime core
must not encode RenderDoc procedures, RenderDoc command names, shader project
names, or business report schemas.

Phase 2 accepts only local filesystem paths. It does not accept artifact ids,
remote URLs, data URLs, or provider-native image content parts as tool input.
Phase 2 also does not snapshot, cache, or copy local image path inputs into
`ArtifactStore`.

Artifact id rejection applies to structured artifact source fields and explicit
artifact URI-style input forms. A bare string in `paths` is always interpreted as
a local filesystem path candidate, not guessed as an artifact id. If that string
does not resolve to an authorized local PNG/JPEG file, normal local-path error
handling applies.

## Tool Definition

Phase 2 exposes `view_image` through `ToolBroker`.

`view_image` is exposed only when the frozen session config snapshot marks
`view_image_enabled = true`. If startup multimodal configuration is missing,
invalid, unsupported, or missing required environment variables, runtime freezes
`view_image_enabled = false`, records a no-secret disabled reason, and omits
`view_image` from `ModelContextFrame.tool_schema_bindings`. The model must not
see or call a disabled `view_image` tool in that session.

Disabled `view_image` is distinct from an unknown tool name. Runtime keeps a
broker-side availability record for disabled `view_image` so stale or direct
valid calls can return the frozen no-secret disabled reason as `config_error`
without routing to `ViewImageTool` or `VisionModelClient`. Unknown tool names
continue to use the existing Phase 1 unknown-tool denial behavior.

Minimum tool metadata:

```json
{
  "name": "view_image",
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "paths": {
      "type": "array",
      "description": "Local filesystem paths to PNG or JPEG images to inspect. Paths are checked by runtime path policy before files are read.",
      "items": {
        "type": "string",
        "description": "A local path to one PNG or JPEG image."
      },
      "minItems": 1,
      "maxItems": 4
    },
    "query": {
      "type": "string",
      "description": "Optional analysis focus for the images, such as what to compare or what visual issue to inspect. If omitted, runtime uses a default image-inspection prompt."
    }
  },
  "required": ["paths"],
  "additionalProperties": false
}
```

`paths` must contain one to four non-empty local path strings. Every path must
resolve to an authorized local PNG or JPEG file. Runtime rejects remote URLs,
`file://` URLs, data URLs, directories, symlinks that resolve outside allowed
scope, missing files, and unsupported or corrupt image files.

`query` is optional in the tool input schema. The multimodal provider call must
always receive an effective query:

- if `query` is present and non-empty after trimming whitespace, runtime uses it
  as the analysis focus.
- if `query` is omitted, runtime uses a fixed default image-inspection query.

Assistant-authored raw tool-call arguments may contain `query`. The immediate
tool-loop transcript may also contain the raw tool call because provider tool
protocols need the model's tool invocation to pair with the tool result.
Starting in Phase 3.5, the human-readable conversation trace may render this
assistant-authored raw tool-call argument as transcript content. Runtime must
not copy the concrete query text into runtime-authored normalized audit,
events/log metadata, status output, error metadata, context-snapshot metadata,
or `ToolResult.metadata` fields.

Runtime trims `query` before semantic validation. `query` has a frozen maximum
length from multimodal configuration, defaulting to 8192 characters after
trimming whitespace. Longer `query` values are invalid tool input and return
`user_error`.

The model cannot provide a custom vision system prompt in Phase 2. Runtime owns
the fixed vision instruction and structured output contract. The effective query
is inserted as bounded task focus inside that runtime-owned instruction; it does
not replace runtime safety, output, uncertainty, or no-fabrication guidance.

Approval scope:

- `view_image` is a read-only native tool for approval-mode purposes.
- Reusable approval grants, when approval mode allows them, use an exact scope
  signature containing `tool_name = "view_image"`, `access = "read"`, and the
  ordered list of canonical image paths after path normalization.
- The scope signature must not include `query`, query source, image MIME type,
  dimensions, byte size, SHA-256, provider, model, timeout, or request-size
  projection. Approval authorizes reading the same local image paths; it does not
  authorize a particular analysis focus or provider call shape.
- Enabling `view_image` through a complete frozen multimodal configuration is
  the runtime contract that authorizes sending approved local image bytes to the
  configured vision provider. `view_image` does not add a separate provider-egress
  approval scope in Phase 2.

Default query:

```text
Describe the visible contents of the image(s), call out visual differences or
anomalies when multiple images are provided, transcribe visible text when
useful, and note uncertainty.
```

## Image Validation And Metadata

`view_image` must compute image metadata for every image source before the
provider call.

Metadata exposed in `ToolResult.output.metadata`:

```json
{
  "path": "relative/or/display/path.png",
  "mime_type": "image/png",
  "width": 1280,
  "height": 720
}
```

Metadata exposed in `ToolResult.metadata.images`, audit events, and trace:

```json
{
  "path": "relative/or/display/path.png",
  "mime_type": "image/png",
  "sha256": "hex",
  "byte_size": 12345,
  "width": 1280,
  "height": 720
}
```

Supported MIME types are:

- `image/png`
- `image/jpeg`

Runtime may store absolute paths in audit metadata when needed, but ordinary
tool output should prefer workspace-relative display paths when available.

Image validation must not trust file extension alone. Runtime must verify image
type from image bytes and parse dimensions through Pillow before the provider
call. Pillow is a Phase 2 runtime dependency because `view_image` owns real image
metadata parsing and future image-format expansion should not duplicate ad hoc
parsers. A file with a supported extension but an unsupported or corrupt byte
signature fails with `tool_error`. A valid PNG or JPEG with an uncommon extension
may be accepted when path policy allows it and byte-level type and dimensions
validate.

Runtime must compute SHA-256 from the exact image bytes sent to the provider.
The hash is audit/runtime metadata, not primary semantic output to the main
agent.

Phase 2 enforces these Kimi-compatible request limits before the provider call:

- every image must have `width <= 4096` and `height <= 4096`.
- every image must have `width * height <= 4096 * 2160`.
- the projected OpenAI-compatible Chat Completions JSON request body must be no
  larger than 100,000,000 bytes after base64 expansion, data URL prefixes, text
  instruction, response format request, and message envelope are included.
  The projection must serialize the compact UTF-8 JSON body that is equivalent
  to the provider wire request. SDK request-extension fields such as
  `extra_body={"thinking": {"type": "disabled"}}` must be merged into the
  projected request body the same way the SDK sends them to the provider. The
  projected body includes `model`, `messages`, `response_format`, `max_tokens`,
  every image data URL content part, the text instruction content part, and any
  required provider-specific request fields after request-extension merging,
  such as top-level `thinking`.

If any image exceeds the dimension or pixel budget, the call fails with
`ToolResult.status = "error"` and `error.error_class = "tool_error"` before the
provider call. If the projected request body exceeds 100,000,000 bytes, the call
also fails with `tool_error` before the provider call.

## Multimodal Configuration

Phase 2 loads multimodal settings from `~/.debug-agent/config.toml` and freezes
their availability and provider facts into `sessions.config_snapshot_json`.

Configuration shape:

```toml
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"
timeout_seconds = 60
max_tokens = 4096
max_query_chars = 8192
max_analysis_chars = 8192

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
```

Phase 2 resolves multimodal configuration at session startup and freezes it into
`sessions.config_snapshot_json`, following the same snapshot strategy as the main
model config. The persisted snapshot must include provider, model, timeout,
max_tokens, max_query_chars, max_analysis_chars, auth environment
variable name, base URL environment variable name, booleans recording whether
those environment variables were present at startup, `view_image_enabled`, and a
no-secret `view_image_disabled_reason` when disabled. Secret values must not be
persisted.

`provider`, `model`, `api_key_env`, and `base_url_env` must be explicitly
configured before a session can expose real `view_image` execution. Runtime may
use built-in defaults only for `timeout_seconds`, `max_tokens`,
`max_query_chars`, and `max_analysis_chars` when they are omitted. Missing or
invalid multimodal config disables `view_image` for the session. It must not fail
session startup by itself. If a required environment variable was present at
startup and `view_image` was enabled, but the variable is missing when
`view_image` executes, the tool call fails with `config_error`.

Automated tests may inject a test-only fake `VisionModelClient` path to exercise
enabled `view_image` behavior without network access or live credentials. This
fake path is not a user-configurable provider, is not read from
`~/.debug-agent/config.toml`, is not a fallback vision path, and must not be
available in ordinary runtime sessions.

Validation rules:

- `provider` must be `openai`.
- Phase 2 supports only `model = "kimi-k2.5"` for real multimodal execution.
- `timeout_seconds`, when configured, must be a positive integer.
- `max_tokens`, when configured, must be a positive integer.
- `max_query_chars`, when configured, must be a positive integer.
- `max_analysis_chars`, when configured, must be a positive integer.
- `api_key_env` must be a non-empty environment variable name.
- `base_url_env` must be a non-empty environment variable name.
- the API key environment variable must be set at session startup and when
  `view_image` executes.
- the base URL environment variable must be set at session startup and when
  `view_image` executes.

If any startup validation rule fails, runtime freezes `view_image_enabled =
false`, records a disabled reason such as `missing_multimodal_config`,
`unsupported_multimodal_provider`, `unsupported_multimodal_model`,
`missing_api_key_env`, or `missing_base_url_env`, and omits `view_image` from the
model-visible tool set. Config and environment changes after startup do not
hot-reload tool availability; users must start a new session to enable or
disable `view_image`.

The configured base URL environment variable is required for real multimodal
execution. Phase 2 must not fall back to the OpenAI-compatible client's built-in
default base URL for `kimi-k2.5`. Users are responsible for configuring a base
URL that is valid for `kimi-k2.5`, such as Moonshot's OpenAI-compatible
endpoint.

The multimodal provider path is separate from the main prompt agent provider
path. Changing main model config must not silently change `view_image` model
selection.

## Vision Request

Runtime calls the OpenAI-compatible Chat Completions API through
`VisionModelClient`. Phase 2 uses the `openai` Python SDK for this
OpenAI-compatible path. The request must follow the Kimi-compatible content-part
shape:

```python
completion = client.chat.completions.create(
    model=vision_model,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,<transient-base64>"}},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<transient-base64>"}},
                {"type": "text", "text": "<runtime-owned instruction with effective query>"}
            ]
        }
    ],
    response_format={"type": "json_object"},
    max_tokens=vision_max_tokens,
    extra_body={"thinking": {"type": "disabled"}},
)
```

The actual request uses one `image_url` content part per input image, followed
by one `text` content part. Multi-image calls send all image parts in the order
supplied by `paths`. The text part must come after the image parts.

The request uses a runtime-owned instruction that asks for a structured JSON
object. The instruction must include the effective query, emphasize that the
model should report uncertainty, and avoid inventing unseen details.
`VisionModelClient` must request `response_format={"type": "json_object"}`. If
the configured OpenAI-compatible provider path cannot support this request shape,
that provider/model path is not valid for Phase 2 real multimodal execution.

Phase 2 disables Kimi thinking for `view_image` requests by sending
`extra_body={"thinking": {"type": "disabled"}}` or the SDK-equivalent request
field. This keeps the provider response contract narrow and JSON-oriented. If a
later phase or approved contract patch enables thinking for better multimodal
analysis, that later change must update the provider response parsing and tests
explicitly.

Required provider JSON object:

```json
{
  "analysis": "Concise answer from the multimodal model."
}
```

The provider may include extra fields, but runtime ignores any provider-returned
source metadata such as path, MIME type, width, height, byte size, or hash.
Runtime source metadata always comes from local validation.

Runtime builds each image content part as a transient data URL:

```text
data:<mime_type>;base64,<base64-encoded-image-bytes>
```

`mime_type` must come from byte-level image validation, not from the file
extension. Supported values are `image/png` and `image/jpeg`.

`VisionModelClient` extracts provider text from
`completion.choices[0].message.content`. Runtime must parse that text as a JSON
object and validate that `analysis` is a non-empty string no longer than the
frozen `max_analysis_chars` limit. Runtime then copies that analysis into
`ToolResult.output.analysis` and attaches runtime-computed image metadata.
Runtime must not trust provider-returned path, width, height, byte size, MIME,
or hash fields as source metadata truth.

`VisionModelClient` uses a non-streaming Chat Completions call in Phase 2. It
must not stream provider deltas to the REPL/TUI or emit ordinary model stream
events for the internal `view_image` provider call. The only model-visible and
user-visible result of `view_image` is the final normalized `ToolResult`.

`view_image` does not implement runtime-level retry in Phase 2. Each
`view_image` tool call performs at most one provider request. SDK/client
implicit retry must be disabled or configured to zero attempts for this provider
call path. Provider timeout, HTTP/SDK failure, malformed response, or
normalization failure returns the corresponding final `ToolResult` for that tool
call. Any business retry decision belongs to the prompt skill or main agent,
which may issue a later new `view_image` tool call.

`view_image` still uses the unified `ToolBroker` timeout envelope. The effective
timeout for the broker-routed tool call comes from the frozen multimodal
`timeout_seconds` setting, defaulting to `60` seconds when omitted. The same
effective timeout must be passed into `VisionModelClient` for the underlying
Chat Completions call. The model-visible `view_image` input schema does not
allow overriding timeout in Phase 2.

Image bytes, base64, and provider image content parts must not be written to:

- `ReplRuntime.conversation`.
- SQLite run event payloads.
- context snapshots.
- trace output.
- events/log output.
- ordinary tool output.

Phase 2 does not persist image bytes or base64 in `ArtifactStore` merely because
`view_image` was called. Large textual provider output, when kept, follows
existing large-output artifact rules.

## Tool Result

On success, `ToolResult.status` is `ok` and `ToolResult.output` is an object:

```json
{
  "analysis": "Concise answer from the multimodal model.",
  "metadata": [
    {
      "path": "path/to/ref.png",
      "mime_type": "image/png",
      "width": 1280,
      "height": 720
    }
  ]
}
```

Fields are required. `metadata` must contain one entry per input image in input
order.

`analysis` must be concise enough for ordinary conversation history and must not
exceed frozen `max_analysis_chars`. If the raw provider text exceeds the existing
large-output threshold, runtime may artifact the raw provider text, but an
over-limit normalized `analysis` remains an invalid provider response and fails
with `model_error`.

`ToolResult.metadata` must include:

```json
{
  "tool_name": "view_image",
  "vision_provider": "openai",
  "vision_model": "kimi-k2.5",
  "duration_ms": 1234,
  "effective_query_source": "default",
  "images": [
    {
      "path": "path/to/ref.png",
      "mime_type": "image/png",
      "sha256": "hex",
      "byte_size": 12345,
      "width": 1280,
      "height": 720
    }
  ]
}
```

`ToolResult.redacted_output` must be either `null` or a short string containing
the analysis and display metadata without raw image content.

`ToolResult.metadata` must not include the concrete effective query text. It
records only whether the effective query came from the runtime default or the
assistant-provided tool input.

`view_image` overrides the generic ToolBroker audit convention that normally
persists normalized arguments. Runtime-authored persisted `view_image` audit
metadata, events/log entries, status output, error metadata, context snapshot
metadata, and `ToolResult.metadata` must not include the concrete effective
query text, raw `query` argument, query previews, or query length facts. They
may record only `effective_query_source` as `assistant` or `default`. This
restriction does not forbid assistant-authored raw tool-call arguments or the
immediate tool-loop transcript from containing `query`. Starting in Phase 3.5,
the conversation trace may render those assistant-authored raw tool-call
arguments as transcript content. Runtime-authored human-readable diagnostics
must follow the redaction rule and must never include image bytes, base64, or
provider image content parts.

## Error Handling

If `view_image_enabled = false` and a stale or direct valid tool call still
reaches `ToolBroker`, runtime must not route to `ViewImageTool` or
`VisionModelClient`. It returns:

- `ToolResult.status = "denied"`
- `error.error_class = "config_error"`
- a `tool_call_denied` event

The error message may include the no-secret disabled reason from the frozen
config snapshot.

If a disabled `view_image` call is malformed, normal Phase 1 broker pre-route or
schema validation applies first and returns `user_error`. If the tool name is
unknown rather than disabled `view_image`, runtime uses the existing Phase 1
unknown-tool denial behavior.

Invalid schema, missing `paths`, too many or too few paths, empty path strings,
unknown fields, a non-string `query`, an empty/whitespace-only `query`, or a
trimmed `query` longer than the frozen `max_query_chars` value:

- `ToolResult.status = "denied"`
- `error.error_class = "user_error"`
- a `tool_call_denied` event

Path policy denial:

- `ToolResult.status = "denied"`
- `error.error_class = "policy_denied"`

Remote URL, `file://` URL, data URL, directory input, missing file, unsupported
image type, corrupt PNG, corrupt JPEG, unreadable image, or symlink escape not
already classified as policy denial:

- `ToolResult.status = "error"`
- `error.error_class = "tool_error"`

Missing API key env var at execution time, frozen multimodal config facts that
cannot construct the OpenAI-compatible vision client, or unsupported frozen
provider/model facts:

- `ToolResult.status = "error"`
- `error.error_class = "config_error"`

Missing or invalid multimodal config discovered at session startup disables
`view_image` for that session instead of producing a startup `config_error`.
Because the disabled tool is omitted from model-visible tool bindings, ordinary
model execution should not produce a `view_image` `ToolResult` for that case.

Provider timeout:

- `ToolResult.status = "timeout"`
- `error.error_class = "timeout"`
- the timeout is recorded through the normal ToolBroker timeout/audit path.

Provider HTTP failure, malformed provider response, provider text that is not a
JSON object, missing, empty, over-limit, or otherwise invalid `analysis`, or
response that cannot be normalized into the required output object:

- `ToolResult.status = "error"`
- `error.error_class = "model_error"`

If any image in a multi-image call fails validation or policy, the entire
`view_image` call fails without invoking the provider. Partial image analysis is
not a Phase 2 contract.

`view_image` failure must not terminalize an active long-lived prompt run.

## Audit And Trace

Every `view_image` call writes audit facts sufficient to explain:

- tool name and tool call id.
- session id and run id.
- normalized source display paths.
- image MIME type, byte size, SHA-256, width, and height when available.
- policy decision and approval decision for each path.
- provider and model.
- effective query source: `assistant` or `default`.
- projected request body size.
- duration.
- status and error class.
- success analysis summary when available.

Trace output must show the same facts at human-readable granularity and must
never show base64 or raw image bytes.

## Conversation And Compression

The normalized `ToolResult.output` is an ordinary durable tool observation.
It may be omitted or compressed by existing context management like other tool
results.

The original image bytes are never ordinary conversation content.

Compression summaries may mention visible observations from earlier
`view_image` tool outputs, but they do not become image metadata truth and do
not replace tool audit facts.

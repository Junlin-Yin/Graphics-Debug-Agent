# Phase 0 Config Specification

## Purpose

Phase 0 reads one global config file and freezes its resolved values into the session config snapshot. Config resolution is intentionally narrow: Phase 0 supports the Anthropic-compatible provider path for real model calls and a fake model path for tests.

Project-local `config.toml` files are not read in Phase 0.

## Config Location

```text
~/.debug-agent/config.toml
```

If the file is absent, runtime still applies built-in defaults for non-provider settings, but real model execution fails with `config_error` unless provider, model, and auth can be resolved from environment-backed configuration.

## Supported Provider

Phase 0 real model execution supports:

- `provider = "anthropic"`

No full provider abstraction is implemented in Phase 0. Provider-specific setup stays inside `ModelFactory`.

Tests may use a fake model path that does not require network access.

## Schema

```toml
[defaults]
provider = "anthropic"
model = "kimi-k2.5"
temperature = 0.2
max_tokens = 8192
timeout_seconds = 120

[auth.anthropic]
api_key_env = "ANTHROPIC_API_KEY"

[providers.anthropic]
base_url_env = "ANTHROPIC_BASE_URL"
default_haiku_model_env = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
default_sonnet_model_env = "ANTHROPIC_DEFAULT_SONNET_MODEL"
default_opus_model_env = "ANTHROPIC_DEFAULT_OPUS_MODEL"
```

## Environment Variables

The following environment variables are the supported Phase 0 Anthropic-compatible configuration surface:

```text
ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic
ANTHROPIC_API_KEY=<secret>
ANTHROPIC_DEFAULT_HAIKU_MODEL=kimi-k2.5
ANTHROPIC_DEFAULT_SONNET_MODEL=kimi-k2.5
ANTHROPIC_DEFAULT_OPUS_MODEL=kimi-k2.6
```

`ANTHROPIC_API_KEY` is the auth token value. `auth.anthropic.api_key_env` names the environment variable that contains the token.

## Resolution Rules

1. Load built-in non-provider defaults for `temperature`, `max_tokens`, `timeout_seconds`, and the Phase 0 system prompt.
2. Load `~/.debug-agent/config.toml` when present.
3. Resolve `provider` from `[defaults].provider`.
4. Resolve `model` from `[defaults].model`.
5. Resolve the Anthropic auth token from the environment variable named by `[auth.anthropic].api_key_env`.
6. Resolve the Anthropic base URL from the environment variable named by `[providers.anthropic].base_url_env` when that field is present.
7. Freeze the resolved values into `sessions.config_snapshot_json` at session creation.

Phase 0 does not guess a real provider or model. Missing or unsupported provider/model/auth configuration returns `config_error`.

## Config Snapshot

The persisted config snapshot includes resolved runtime settings and provider metadata, but it must not persist secret values. The snapshot may include the auth environment variable name and whether the token was present.

Example:

```json
{
  "provider": "anthropic",
  "model": "kimi-k2.5",
  "temperature": 0.2,
  "max_tokens": 8192,
  "timeout_seconds": 120,
  "auth": {
    "api_key_env": "ANTHROPIC_API_KEY",
    "api_key_present": true
  },
  "provider_settings": {
    "base_url_env": "ANTHROPIC_BASE_URL",
    "base_url_present": true
  }
}
```

# ADR 0006: Frozen Session Config Snapshot And Narrow Provider Strategy

## Status

Accepted after Phase 0 implementation.

## Context

Long-running debug sessions need reproducible runtime behavior. Model provider,
model name, timeout, token limits, and system prompt choices affect execution
and must be explainable after the session completes or fails.

At the same time, Phase 0 should not implement a full provider abstraction or
configuration control plane. The project contract keeps early provider support
narrow and explicitly rejects skill, agent, config, and model hot reload for v1.

## Decision

Resolve runtime configuration once at session startup and persist a frozen
session config snapshot.

Phase 0 reads the global config file at `~/.debug-agent/config.toml` when
present. It applies built-in defaults for non-provider settings, but it does not
guess a real provider or model. Missing or unsupported provider, model, or auth
configuration produces `config_error`.

Phase 0 supports one real LangChain-compatible provider path:

- `provider = "anthropic"`

Tests may use a deterministic fake model path. The fake path is a test and
acceptance mechanism, not a general provider abstraction.

The persisted snapshot may include provider metadata, environment variable
names, and whether auth was present. It must not persist secret values.

Provider-specific construction stays inside `ModelFactory`. Runtime contracts
and persisted session state should not depend on provider-specific model
classes.

## Alternatives Considered

### Guess a default provider or model

This improves first-run convenience, but makes failures harder to understand and
can accidentally call an unintended external model.

### Read project-local config in Phase 0

Project-local config may be useful later, but Phase 0 keeps configuration
surface narrow to avoid precedence rules, workspace trust questions, and hidden
behavior changes.

### Re-read config before every model or tool call

This allows hot changes, but makes long sessions non-reproducible and complicates
trace interpretation and resume behavior.

### Implement a full provider abstraction immediately

This is more extensible, but Phase 0 only needs one stable real provider path to
prove the runtime contracts and adapter boundary.

## Consequences

- Each session records the configuration facts needed to explain its behavior.
- Secret values stay outside persisted runtime state.
- Config changes require a new session to take effect.
- Model provider expansion must be explicit and should not leak provider
  details into runtime contracts.
- Tests remain network-free through the fake model path.

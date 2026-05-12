from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PHASE_0_SYSTEM_PROMPT = (
    "You are debug-agent, a local debugging assistant. Answer concisely and use "
    "only tools exposed by the runtime."
)

NON_PROVIDER_DEFAULTS: dict[str, Any] = {
    "temperature": 0.2,
    "max_tokens": 8192,
    "timeout_seconds": 120,
    "system_prompt": PHASE_0_SYSTEM_PROMPT,
}


@dataclass(frozen=True)
class ConfigError:
    error_class: str
    message: str
    source: str
    recoverable: bool


@dataclass(frozen=True)
class ConfigLoadResult:
    snapshot: dict[str, Any] | None
    error: ConfigError | None
    defaults: dict[str, Any]


def load_config_snapshot(config_path: Path | None = None) -> ConfigLoadResult:
    path = config_path or _default_config_path()
    defaults = dict(NON_PROVIDER_DEFAULTS)
    raw_config: dict[str, Any] = {}

    if path.exists():
        try:
            raw_config = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            return ConfigLoadResult(
                snapshot=None,
                error=_config_error(f"Invalid config.toml: {exc}"),
                defaults=defaults,
            )

    config_defaults = raw_config.get("defaults", {})
    provider = config_defaults.get("provider")
    model = config_defaults.get("model")

    if not provider or not model:
        return ConfigLoadResult(
            snapshot=None,
            error=_config_error("Provider and model must be configured for Phase 0."),
            defaults=defaults,
        )

    if provider == "fake":
        runtime_settings = _resolve_runtime_settings(config_defaults, defaults)
        snapshot = {
            "provider": provider,
            "model": model,
            **runtime_settings,
            "fake_response": config_defaults.get("fake_response", "fake response"),
        }
        return ConfigLoadResult(snapshot=snapshot, error=None, defaults=defaults)

    if provider != "anthropic":
        return ConfigLoadResult(
            snapshot=None,
            error=_config_error(f"Unsupported provider for Phase 0: {provider}"),
            defaults=defaults,
        )

    runtime_settings = _resolve_runtime_settings(config_defaults, defaults)
    auth_config = raw_config.get("auth", {}).get("anthropic", {})
    api_key_env = auth_config.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key_present = bool(os.environ.get(api_key_env))
    if not api_key_present:
        return ConfigLoadResult(
            snapshot=None,
            error=_config_error(f"Missing auth token in environment variable: {api_key_env}"),
            defaults=defaults,
        )

    provider_config = raw_config.get("providers", {}).get("anthropic", {})
    base_url_env = provider_config.get("base_url_env", "ANTHROPIC_BASE_URL")

    snapshot = {
        "provider": provider,
        "model": model,
        **runtime_settings,
        "auth": {
            "api_key_env": api_key_env,
            "api_key_present": api_key_present,
        },
        "provider_settings": {
            "base_url_env": base_url_env,
            "base_url_present": bool(os.environ.get(base_url_env)),
        },
    }
    return ConfigLoadResult(snapshot=snapshot, error=None, defaults=defaults)


def _resolve_runtime_settings(
    config_defaults: dict[str, Any], builtins: dict[str, Any]
) -> dict[str, Any]:
    return {
        "temperature": config_defaults.get("temperature", builtins["temperature"]),
        "max_tokens": config_defaults.get("max_tokens", builtins["max_tokens"]),
        "timeout_seconds": config_defaults.get(
            "timeout_seconds", builtins["timeout_seconds"]
        ),
        "system_prompt": builtins["system_prompt"],
    }


def _default_config_path() -> Path:
    home = os.environ.get("DEBUG_AGENT_HOME") or os.environ.get("HOME")
    if home:
        return Path(home) / ".debug-agent" / "config.toml"
    return Path.home() / ".debug-agent" / "config.toml"


def _config_error(message: str) -> ConfigError:
    return ConfigError(
        error_class="config_error",
        message=message,
        source="config",
        recoverable=True,
    )

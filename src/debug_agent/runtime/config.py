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

CONTEXT_DEFAULTS: dict[str, Any] = {
    "window_tokens": 200000,
    "omit_old_tool_results_at_ratio": 0.60,
    "compress_history_at_ratio": 0.80,
    "retain_recent_model_calls": 4,
    "compression_reserved_output_tokens": 10000,
}

EXECUTION_DEFAULTS: dict[str, Any] = {
    "default_shell_timeout_seconds": 300,
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
    context_result = _resolve_context_settings(raw_config.get("context", {}))
    if isinstance(context_result, ConfigError):
        return ConfigLoadResult(snapshot=None, error=context_result, defaults=defaults)
    execution_result = _resolve_execution_settings(raw_config.get("execution", {}))
    if isinstance(execution_result, ConfigError):
        return ConfigLoadResult(snapshot=None, error=execution_result, defaults=defaults)
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
            "context": context_result,
            "execution": execution_result,
            "fake_response": config_defaults.get("fake_response", "fake response"),
        }
        if "fake_error" in config_defaults:
            snapshot["fake_error"] = config_defaults["fake_error"]
        if "fake_timeout" in config_defaults:
            snapshot["fake_timeout"] = config_defaults["fake_timeout"]
        if "fake_cancelled" in config_defaults:
            snapshot["fake_cancelled"] = config_defaults["fake_cancelled"]
        if "fake_stream_chunks" in config_defaults:
            snapshot["fake_stream_chunks"] = config_defaults["fake_stream_chunks"]
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
        "context": context_result,
        "execution": execution_result,
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


def _resolve_context_settings(raw_context: Any) -> dict[str, Any] | ConfigError:
    if raw_context is None:
        raw_context = {}
    if not isinstance(raw_context, dict):
        return _config_error("[context] must be a table.")
    settings = {**CONTEXT_DEFAULTS, **raw_context}
    window_tokens = settings.get("window_tokens")
    omit_ratio = settings.get("omit_old_tool_results_at_ratio")
    compress_ratio = settings.get("compress_history_at_ratio")
    retain_recent = settings.get("retain_recent_model_calls")
    reserved_output = settings.get("compression_reserved_output_tokens")
    if not isinstance(window_tokens, int) or window_tokens <= 0:
        return _config_error("context.window_tokens must be a positive integer.")
    if not isinstance(omit_ratio, (int, float)) or omit_ratio <= 0 or omit_ratio > 1:
        return _config_error(
            "context.omit_old_tool_results_at_ratio must be greater than 0 and at most 1."
        )
    if (
        not isinstance(compress_ratio, (int, float))
        or compress_ratio <= 0
        or compress_ratio > 1
    ):
        return _config_error(
            "context.compress_history_at_ratio must be greater than 0 and at most 1."
        )
    if float(omit_ratio) > float(compress_ratio):
        return _config_error(
            "context.omit_old_tool_results_at_ratio must be less than or equal to "
            "context.compress_history_at_ratio."
        )
    if not isinstance(retain_recent, int) or retain_recent < 0:
        return _config_error(
            "context.retain_recent_model_calls must be a non-negative integer."
        )
    if (
        not isinstance(reserved_output, int)
        or reserved_output < 0
        or reserved_output >= window_tokens
    ):
        return _config_error(
            "context.compression_reserved_output_tokens must be a non-negative "
            "integer less than context.window_tokens."
        )
    return {
        "window_tokens": window_tokens,
        "omit_old_tool_results_at_ratio": float(omit_ratio),
        "compress_history_at_ratio": float(compress_ratio),
        "retain_recent_model_calls": retain_recent,
        "compression_reserved_output_tokens": reserved_output,
    }


def _resolve_execution_settings(raw_execution: Any) -> dict[str, Any] | ConfigError:
    if raw_execution is None:
        raw_execution = {}
    if not isinstance(raw_execution, dict):
        return _config_error("[execution] must be a table.")
    settings = {**EXECUTION_DEFAULTS, **raw_execution}
    timeout = settings.get("default_shell_timeout_seconds")
    if not isinstance(timeout, int) or timeout <= 0:
        return _config_error(
            "execution.default_shell_timeout_seconds must be a positive integer."
        )
    return {"default_shell_timeout_seconds": timeout}

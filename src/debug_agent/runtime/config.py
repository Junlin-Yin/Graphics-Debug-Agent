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

MULTIMODAL_LIMIT_DEFAULTS: dict[str, int] = {
    "timeout_seconds": 60,
    "max_tokens": 4096,
    "max_query_chars": 8192,
    "max_analysis_chars": 8192,
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
    multimodal_result = _resolve_multimodal_settings(raw_config.get("multimodal"))
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
            "multimodal": multimodal_result,
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
        if "fake_tool_calls" in config_defaults:
            snapshot["fake_tool_calls"] = config_defaults["fake_tool_calls"]
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
        "multimodal": multimodal_result,
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


def _resolve_multimodal_settings(raw_multimodal: Any) -> dict[str, Any]:
    if not isinstance(raw_multimodal, dict):
        return _disabled_multimodal_snapshot(
            "missing_multimodal_config",
            provider=None,
            model=None,
            api_key_env=None,
            base_url_env=None,
        )

    defaults = raw_multimodal.get("defaults")
    auth = raw_multimodal.get("auth")
    providers = raw_multimodal.get("providers")
    openai_provider = (
        providers.get("openai")
        if isinstance(providers, dict) and isinstance(providers.get("openai"), dict)
        else None
    )
    if not isinstance(defaults, dict) or not isinstance(auth, dict) or openai_provider is None:
        return _disabled_multimodal_snapshot(
            "missing_multimodal_config",
            provider=_string_or_none(defaults.get("provider"))
            if isinstance(defaults, dict)
            else None,
            model=_string_or_none(defaults.get("model"))
            if isinstance(defaults, dict)
            else None,
            api_key_env=_string_or_none(auth.get("api_key_env"))
            if isinstance(auth, dict)
            else None,
            base_url_env=_string_or_none(openai_provider.get("base_url_env"))
            if isinstance(openai_provider, dict)
            else None,
        )

    provider = _string_or_none(defaults.get("provider"))
    model = _string_or_none(defaults.get("model"))
    api_key_env = _string_or_none(auth.get("api_key_env"))
    base_url_env = _string_or_none(openai_provider.get("base_url_env"))
    limits, invalid_reason = _resolve_multimodal_limits(defaults)
    if invalid_reason is not None:
        return _disabled_multimodal_snapshot(
            invalid_reason,
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
            limits=limits,
        )
    if not provider:
        reason = "missing_multimodal_config"
    elif provider != "openai":
        reason = "unsupported_multimodal_provider"
    elif not model:
        reason = "missing_multimodal_config"
    elif model != "kimi-k2.5":
        reason = "unsupported_multimodal_model"
    elif not api_key_env:
        reason = "missing_api_key_env"
    elif not base_url_env:
        reason = "missing_base_url_env"
    elif not os.environ.get(api_key_env):
        reason = "missing_api_key_env"
    elif not os.environ.get(base_url_env):
        reason = "missing_base_url_env"
    else:
        reason = None

    if reason is not None:
        return _disabled_multimodal_snapshot(
            reason,
            provider=provider,
            model=model,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
            limits=limits,
        )
    return {
        "provider": provider,
        "model": model,
        **limits,
        "api_key_env": api_key_env,
        "api_key_present": True,
        "base_url_env": base_url_env,
        "base_url_present": True,
        "view_image_enabled": True,
        "view_image_disabled_reason": None,
    }


def _resolve_multimodal_limits(defaults: dict[str, Any]) -> tuple[dict[str, int], str | None]:
    limits: dict[str, int] = {}
    for key, builtin in MULTIMODAL_LIMIT_DEFAULTS.items():
        value = defaults.get(key, builtin)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return {**MULTIMODAL_LIMIT_DEFAULTS, **limits}, f"invalid_{key}"
        limits[key] = value
    return limits, None


def _disabled_multimodal_snapshot(
    reason: str,
    *,
    provider: str | None,
    model: str | None,
    api_key_env: str | None,
    base_url_env: str | None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    resolved_limits = dict(MULTIMODAL_LIMIT_DEFAULTS)
    if limits is not None:
        resolved_limits.update(limits)
    return {
        "provider": provider,
        "model": model,
        **resolved_limits,
        "api_key_env": api_key_env,
        "api_key_present": bool(api_key_env and os.environ.get(api_key_env)),
        "base_url_env": base_url_env,
        "base_url_present": bool(base_url_env and os.environ.get(base_url_env)),
        "view_image_enabled": False,
        "view_image_disabled_reason": reason,
    }


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None

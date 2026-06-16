import pytest

from debug_agent.runtime.config import ConfigError, load_config_snapshot


def test_absent_config_applies_non_provider_defaults_without_guessing_provider(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error == ConfigError(
        error_class="config_error",
        message="Provider and model must be configured for Phase 0.",
        source="config",
        recoverable=True,
    )
    assert result.defaults["temperature"] == 0.2
    assert result.defaults["max_tokens"] == 8192
    assert result.defaults["timeout_seconds"] == 120
    assert result.defaults["system_prompt"].startswith("You are debug-agent")


def test_default_config_path_honors_home_environment_on_all_platforms(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "openai"
model = "gpt-test"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is not None
    assert "Unsupported provider" in result.error.message


def test_config_snapshot_resolves_anthropic_without_persisting_secret(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "anthropic"
model = "kimi-k2.5"
temperature = 0.1
max_tokens = 4096
timeout_seconds = 60

[auth.anthropic]
api_key_env = "ANTHROPIC_API_KEY"

[providers.anthropic]
base_url_env = "ANTHROPIC_BASE_URL"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.test")

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot == {
        "provider": "anthropic",
        "model": "kimi-k2.5",
        "temperature": 0.1,
        "max_tokens": 4096,
        "timeout_seconds": 60,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer "
            "concisely and use only tools exposed by the runtime."
        ),
        "auth": {
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_key_present": True,
        },
        "provider_settings": {
            "base_url_env": "ANTHROPIC_BASE_URL",
            "base_url_present": True,
        },
        "multimodal": {
            "provider": None,
            "model": None,
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 8192,
            "max_analysis_chars": 8192,
            "api_key_env": None,
            "api_key_present": False,
            "base_url_env": None,
            "base_url_present": False,
            "view_image_enabled": False,
            "view_image_disabled_reason": "missing_multimodal_config",
        },
        "context": {
            "window_tokens": 200000,
            "omit_old_tool_results_at_ratio": 0.60,
            "compress_history_at_ratio": 0.80,
            "retain_recent_model_calls": 4,
            "compression_reserved_output_tokens": 10000,
        },
        "execution": {
            "default_tool_timeout_seconds": 30,
            "default_shell_timeout_seconds": 300,
            "max_shell_timeout_seconds": 3600,
            "cancellation_timeout_seconds": 10,
        },
        "agent_loop": {"max_tool_call_iterations": 1000},
        "development": {
            "allow_incomplete_phase3_prompt_execution": False,
        },
        "thinking": {"enabled": False, "effort": "high"},
    }
    assert "secret-value" not in str(result.snapshot)


def test_config_snapshot_allows_fake_provider_for_tests(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "hello"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot == {
        "provider": "fake",
        "model": "fake-model",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer "
            "concisely and use only tools exposed by the runtime."
        ),
        "context": {
            "window_tokens": 200000,
            "omit_old_tool_results_at_ratio": 0.60,
            "compress_history_at_ratio": 0.80,
            "retain_recent_model_calls": 4,
            "compression_reserved_output_tokens": 10000,
        },
        "execution": {
            "default_tool_timeout_seconds": 30,
            "default_shell_timeout_seconds": 300,
            "max_shell_timeout_seconds": 3600,
            "cancellation_timeout_seconds": 10,
        },
        "agent_loop": {"max_tool_call_iterations": 1000},
        "development": {
            "allow_incomplete_phase3_prompt_execution": False,
        },
        "multimodal": {
            "provider": None,
            "model": None,
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 8192,
            "max_analysis_chars": 8192,
            "api_key_env": None,
            "api_key_present": False,
            "base_url_env": None,
            "base_url_present": False,
            "view_image_enabled": False,
            "view_image_disabled_reason": "missing_multimodal_config",
        },
        "thinking": {"enabled": False, "effort": "high"},
        "fake_response": "hello",
    }


def test_config_snapshot_preserves_fake_stream_chunks_for_tui_smoke(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "unused"
fake_stream_chunks = ["stream", " answer"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot["fake_stream_chunks"] == ["stream", " answer"]


def test_phase3_execution_timeout_config_freezes_defaults_and_validates_order(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "hello"

[execution]
default_tool_timeout_seconds = 33
default_shell_timeout_seconds = 120
max_shell_timeout_seconds = 600
cancellation_timeout_seconds = 7

[agent_loop]
max_tool_call_iterations = 1234
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot["execution"] == {
        "default_tool_timeout_seconds": 33,
        "default_shell_timeout_seconds": 120,
        "max_shell_timeout_seconds": 600,
        "cancellation_timeout_seconds": 7,
    }
    assert result.snapshot["agent_loop"] == {"max_tool_call_iterations": 1234}


def test_phase35_config_accepts_very_large_agent_loop_and_generic_tool_timeout(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    huge = 10**18
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"

[agent_loop]
max_tool_call_iterations = {huge}

[execution]
default_tool_timeout_seconds = {huge}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot["agent_loop"]["max_tool_call_iterations"] == huge
    assert result.snapshot["execution"]["default_tool_timeout_seconds"] == huge


def test_phase35_config_ignores_unrecognized_multimodal_fixed_limit_fields(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"

[multimodal.defaults]
max_images = 999
max_image_edge = 999
max_image_pixels = 999
max_request_bytes = 999
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert "max_images" not in result.snapshot["multimodal"]
    assert "max_image_edge" not in result.snapshot["multimodal"]
    assert "max_image_pixels" not in result.snapshot["multimodal"]
    assert "max_request_bytes" not in result.snapshot["multimodal"]


@pytest.mark.parametrize(
    ("toml_section", "expected_message"),
    [
        (
            "[agent_loop]\nmax_tool_call_iterations = 0",
            "agent_loop.max_tool_call_iterations must be a positive integer.",
        ),
        (
            "[agent_loop]\nmax_tool_call_iterations = -1",
            "agent_loop.max_tool_call_iterations must be a positive integer.",
        ),
        (
            '[agent_loop]\nmax_tool_call_iterations = "1000"',
            "agent_loop.max_tool_call_iterations must be a positive integer.",
        ),
        (
            "[agent_loop]\nmax_tool_call_iterations = true",
            "agent_loop.max_tool_call_iterations must be a positive integer.",
        ),
        (
            "[execution]\ndefault_tool_timeout_seconds = 0",
            "execution.default_tool_timeout_seconds must be a positive integer.",
        ),
        (
            "[execution]\ndefault_tool_timeout_seconds = -1",
            "execution.default_tool_timeout_seconds must be a positive integer.",
        ),
        (
            '[execution]\ndefault_tool_timeout_seconds = "30"',
            "execution.default_tool_timeout_seconds must be a positive integer.",
        ),
        (
            "[execution]\ndefault_tool_timeout_seconds = false",
            "execution.default_tool_timeout_seconds must be a positive integer.",
        ),
    ],
)
def test_phase35_config_rejects_invalid_agent_loop_and_generic_tool_timeout(
    tmp_path, monkeypatch, toml_section, expected_message
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"

{toml_section}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error == ConfigError(
        error_class="config_error",
        message=expected_message,
        source="config",
        recoverable=True,
    )


def test_phase3_execution_timeout_config_rejects_invalid_maximum(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"

[execution]
default_shell_timeout_seconds = 300
max_shell_timeout_seconds = 299
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error == ConfigError(
        error_class="config_error",
        message=(
            "execution.max_shell_timeout_seconds must be greater than or equal "
            "to execution.default_shell_timeout_seconds."
        ),
        source="config",
        recoverable=True,
    )


def test_unsupported_provider_returns_config_error(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "openai"
model = "gpt-test"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.error_class == "config_error"
    assert "Unsupported provider" in result.error.message


def test_missing_anthropic_auth_returns_config_error(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "anthropic"
model = "kimi-k2.5"

[auth.anthropic]
api_key_env = "ANTHROPIC_API_KEY"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.error_class == "config_error"
    assert "ANTHROPIC_API_KEY" in result.error.message

from __future__ import annotations


def test_settings_modules_import_and_expose_documented_groups() -> None:
    from debug_agent.cli import settings as cli_settings
    from debug_agent.persistence import settings as persistence_settings
    from debug_agent.runtime import settings as runtime_settings
    from debug_agent.tools import settings as tool_settings

    assert runtime_settings.NON_PROVIDER_DEFAULTS["system_prompt"].startswith(
        "You are debug-agent"
    )
    assert runtime_settings.CONTEXT_DEFAULTS["window_tokens"] == 200000
    assert runtime_settings.EXECUTION_DEFAULTS["default_tool_timeout_seconds"] == 30
    assert runtime_settings.EXECUTION_DEFAULTS["max_shell_timeout_seconds"] == 3600
    assert runtime_settings.AGENT_LOOP_DEFAULTS["max_tool_call_iterations"] == 1000
    assert runtime_settings.MAX_TOOL_CALL_ITERATIONS == 1000
    assert runtime_settings.TOKEN_ESTIMATOR_VERSION == "deterministic-char-v1"
    assert ".sessions" in runtime_settings.BUILTIN_DIRECTORY_DENIES

    assert tool_settings.DEFAULT_NATIVE_TOOL_LIMIT == 1000
    assert tool_settings.DEFAULT_TOOL_TIMEOUT_SECONDS == 30.0
    assert tool_settings.MAX_VIEW_IMAGE_DIMENSION == 4096
    assert tool_settings.MAX_VIEW_IMAGE_REQUEST_BODY_BYTES == 100_000_000

    assert "debug-agent" in cli_settings.USAGE
    assert "normal" in cli_settings.APPROVAL_MODES
    assert cli_settings.MESSAGE_SCROLL_STEP_PAGE == 10
    assert cli_settings.MAX_MARKDOWN_RENDER_CHARS == 50_000

    assert persistence_settings.PHASE_3_SCHEMA_USER_VERSION == 3
    assert persistence_settings.PHASE_3_5_SCHEMA_USER_VERSION == 4
    assert 0 in persistence_settings.LEGACY_SCHEMA_USER_VERSIONS
    assert 3 in persistence_settings.PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS
    assert "CREATE TABLE IF NOT EXISTS sessions" in persistence_settings.SQLITE_SCHEMA
    assert persistence_settings.TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION == 1
    assert persistence_settings.PHASE_3_5_TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION == 2
    assert persistence_settings.SNAPSHOT_INLINE_THRESHOLD_BYTES == 16 * 1024

from __future__ import annotations

import json

from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator
from debug_agent.tools.native import gated_user_facing_tool_definitions


def _write_config(home, body: str) -> None:
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(body.strip(), encoding="utf-8")


def _base_fake_config(multimodal_body: str = "") -> str:
    return f"""
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "done"

{multimodal_body}
"""


def test_missing_multimodal_config_freezes_disabled_view_image_without_startup_error(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_config(home, _base_fake_config())
    monkeypatch.setenv("HOME", str(home))

    config = load_config_snapshot()
    assert config.error is None
    assert config.snapshot is not None
    multimodal = config.snapshot["multimodal"]

    assert multimodal == {
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
    }

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        config.snapshot,
        approval_mode="yolo",
    )
    assert result.exit_code == 0
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        session = SessionStore(db.connection).get(result.session_id)
        persisted = session.config_snapshot["multimodal"]
        assert persisted == multimodal
        assert "secret" not in json.dumps(session.config_snapshot, sort_keys=True)
    finally:
        db.close()


def test_complete_multimodal_config_freezes_enabled_ready_but_keeps_tool_gated(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(
        home,
        _base_fake_config(
            """
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
"""
        ),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://moonshot.invalid/v1")

    config = load_config_snapshot()

    assert config.error is None
    assert config.snapshot is not None
    assert config.snapshot["multimodal"] == {
        "provider": "openai",
        "model": "kimi-k2.5",
        "timeout_seconds": 60,
        "max_tokens": 4096,
        "max_query_chars": 8192,
        "max_analysis_chars": 8192,
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key_present": True,
        "base_url_env": "MOONSHOT_BASE_URL",
        "base_url_present": True,
        "view_image_enabled": True,
        "view_image_disabled_reason": None,
    }
    visible_names = [definition.name for definition in gated_user_facing_tool_definitions()]
    assert "todo" in visible_names
    assert "view_image" not in visible_names
    assert "secret-key" not in json.dumps(config.snapshot, sort_keys=True)


def test_required_multimodal_facts_must_be_explicit_before_enabled_ready(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(
        home,
        _base_fake_config(
            """
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
"""
        ),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://moonshot.invalid/v1")

    config = load_config_snapshot()

    assert config.error is None
    assert config.snapshot is not None
    assert config.snapshot["multimodal"]["view_image_enabled"] is False
    assert (
        config.snapshot["multimodal"]["view_image_disabled_reason"]
        == "missing_base_url_env"
    )


def test_unsupported_multimodal_provider_and_model_disable_view_image(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(
        home,
        _base_fake_config(
            """
[multimodal.defaults]
provider = "anthropic"
model = "claude-vision"

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
"""
        ),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://moonshot.invalid/v1")

    provider_config = load_config_snapshot()
    assert provider_config.error is None
    assert provider_config.snapshot is not None
    assert (
        provider_config.snapshot["multimodal"]["view_image_disabled_reason"]
        == "unsupported_multimodal_provider"
    )

    (home / ".debug-agent" / "config.toml").write_text(
        _base_fake_config(
            """
[multimodal.defaults]
provider = "openai"
model = "gpt-vision"

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
"""
        ).strip(),
        encoding="utf-8",
    )

    model_config = load_config_snapshot()
    assert model_config.error is None
    assert model_config.snapshot is not None
    assert (
        model_config.snapshot["multimodal"]["view_image_disabled_reason"]
        == "unsupported_multimodal_model"
    )


def test_multimodal_limits_must_be_positive_integers_and_do_not_hide_other_tools(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://moonshot.invalid/v1")

    for setting in (
        "timeout_seconds",
        "max_tokens",
        "max_query_chars",
        "max_analysis_chars",
    ):
        limits = {
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 8192,
            "max_analysis_chars": 8192,
        }
        limits[setting] = 0
        _write_config(
            home,
            _base_fake_config(
                f"""
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"
timeout_seconds = {limits["timeout_seconds"]}
max_tokens = {limits["max_tokens"]}
max_query_chars = {limits["max_query_chars"]}
max_analysis_chars = {limits["max_analysis_chars"]}

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
"""
            ),
        )

        config = load_config_snapshot()

        assert config.error is None
        assert config.snapshot is not None
        assert config.snapshot["multimodal"]["view_image_enabled"] is False
        assert (
            config.snapshot["multimodal"]["view_image_disabled_reason"]
            == f"invalid_{setting}"
        )
    visible_names = [definition.name for definition in gated_user_facing_tool_definitions()]
    assert "todo" in visible_names
    assert "read_file" in visible_names
    assert "view_image" not in visible_names


def test_multimodal_env_presence_is_frozen_at_config_load(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(
        home,
        _base_fake_config(
            """
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"
timeout_seconds = 45
max_tokens = 2048
max_query_chars = 1000
max_analysis_chars = 2000

[multimodal.auth]
api_key_env = "MOONSHOT_API_KEY"

[multimodal.providers.openai]
base_url_env = "MOONSHOT_BASE_URL"
"""
        ),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://moonshot.invalid/v1")

    config = load_config_snapshot()
    monkeypatch.setenv("MOONSHOT_API_KEY", "later-secret")

    assert config.error is None
    assert config.snapshot is not None
    assert config.snapshot["multimodal"] == {
        "provider": "openai",
        "model": "kimi-k2.5",
        "timeout_seconds": 45,
        "max_tokens": 2048,
        "max_query_chars": 1000,
        "max_analysis_chars": 2000,
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key_present": False,
        "base_url_env": "MOONSHOT_BASE_URL",
        "base_url_present": True,
        "view_image_enabled": False,
        "view_image_disabled_reason": "missing_api_key_env",
    }

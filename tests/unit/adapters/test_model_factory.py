from __future__ import annotations

import subprocess
import sys

from debug_agent.adapters.model_factory import FakeChatModel, ModelFactory


def test_model_factory_fake_provider_does_not_import_anthropic_dependency() -> None:
    script = """
import importlib.abc
import sys

class BlockAnthropic(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "langchain_anthropic" or fullname.startswith("langchain_anthropic."):
            raise RuntimeError("langchain_anthropic must not be imported")
        return None

sys.meta_path.insert(0, BlockAnthropic())
from debug_agent.adapters.model_factory import ModelFactory

result = ModelFactory().create({
    "provider": "fake",
    "model": "fake-model",
    "fake_response": "hello",
})
assert result.error is None
assert result.model.invoke([]).content == "hello"
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_model_factory_creates_fake_model_for_tests() -> None:
    result = ModelFactory().create(
        {
            "provider": "fake",
            "model": "fake-model",
            "fake_response": "hello from fake",
        }
    )

    assert result.error is None
    assert isinstance(result.model, FakeChatModel)
    assert result.model.invoke([]).content == "hello from fake"


def test_model_factory_returns_config_error_for_unsupported_provider() -> None:
    result = ModelFactory().create({"provider": "openai", "model": "gpt"})

    assert result.model is None
    assert result.error == {
        "error_class": "config_error",
        "message": "Unsupported provider for Phase 0: openai",
        "source": "model_factory",
        "recoverable": True,
    }


def test_model_factory_returns_config_error_when_auth_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = ModelFactory().create(
        {
            "provider": "anthropic",
            "model": "kimi-k2.5",
            "temperature": 0.2,
            "max_tokens": 8192,
            "timeout_seconds": 120,
            "auth": {
                "api_key_env": "ANTHROPIC_API_KEY",
                "api_key_present": True,
            },
            "provider_settings": {"base_url_env": "ANTHROPIC_BASE_URL"},
        }
    )

    assert result.model is None
    assert result.error["error_class"] == "config_error"
    assert "ANTHROPIC_API_KEY" in result.error["message"]


def test_model_factory_constructs_anthropic_model_without_exposing_secret(
    monkeypatch,
) -> None:
    captured = {}

    class DummyChatAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "debug_agent.adapters.model_factory._load_chat_anthropic",
        lambda: DummyChatAnthropic,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.test")

    result = ModelFactory().create(
        {
            "provider": "anthropic",
            "model": "kimi-k2.5",
            "temperature": 0.2,
            "max_tokens": 8192,
            "timeout_seconds": 120,
            "auth": {
                "api_key_env": "ANTHROPIC_API_KEY",
                "api_key_present": True,
            },
            "provider_settings": {"base_url_env": "ANTHROPIC_BASE_URL"},
        }
    )

    assert result.error is None
    assert isinstance(result.model, DummyChatAnthropic)
    assert captured == {
        "model_name": "kimi-k2.5",
        "temperature": 0.2,
        "max_tokens_to_sample": 8192,
        "timeout": 120,
        "api_key": "secret-token",
        "base_url": "https://api.example.test",
    }
    assert "secret-token" not in str(result)

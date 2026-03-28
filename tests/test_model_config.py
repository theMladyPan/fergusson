import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.core import resolve_model_spec
from src.config import Settings, load_config


def test_settings_load_model_specs_from_env(monkeypatch):
    monkeypatch.setenv("SMART_MODEL", "google-gla:gemini-3-pro-preview")
    monkeypatch.setenv("FAST_MODEL", "openai:gpt-4.1-mini")

    settings = Settings(_env_file=None)

    assert settings.smart_model == "google-gla:gemini-3-pro-preview"
    assert settings.fast_model == "openai:gpt-4.1-mini"


def test_load_config_ignores_legacy_model_sections(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "providers": {
    "legacy": {
      "type": "openai"
    }
  },
  "models": {
    "smart": {
      "provider": "legacy",
      "model": "gpt-4.1"
    }
  },
  "channels": {
    "discord": {
      "enabled": true
    }
  },
  "mcp_servers": {
    "demo": {
      "command": "npx",
      "args": ["-y", "demo"],
      "tool_timeout": 30
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    app_config = load_config(config_path)

    assert "discord" in app_config.channels
    assert app_config.channels["discord"].enabled is True
    assert "demo" in app_config.mcp_servers


def test_resolve_model_spec_wraps_openai(monkeypatch):
    monkeypatch.setattr("src.agent.core.logfire.instrument_httpx", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.agent.core.logfire.instrument_openai", lambda *args, **kwargs: None)

    client = AsyncClient()
    try:
        model = resolve_model_spec("openai:gpt-4.1", retrying_client=client)
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "gpt-4.1"


def test_resolve_model_spec_wraps_google_gla(monkeypatch):
    monkeypatch.setattr("src.agent.core.logfire.instrument_httpx", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.agent.core.logfire.instrument_google_genai", lambda *args, **kwargs: None)

    client = AsyncClient()
    try:
        model = resolve_model_spec("google-gla:gemini-3-pro-preview", retrying_client=client)
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert isinstance(model, GoogleModel)
    assert model.model_name == "gemini-3-pro-preview"


def test_resolve_model_spec_passes_through_native_strings():
    model = resolve_model_spec("gateway/openai:gpt-5.2")
    assert model == "gateway/openai:gpt-5.2"


def test_resolve_model_spec_rejects_invalid_strings():
    with pytest.raises(ValueError, match="provider:model"):
        resolve_model_spec("not-a-model-spec")

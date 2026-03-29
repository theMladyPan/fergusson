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


def test_settings_load_neo4j_from_env(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.databases.neo4j.io")
    monkeypatch.setenv("NEO4J_USER", "neo4j-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "aura-db")

    settings = Settings(_env_file=None)

    assert settings.neo4j.uri == "neo4j+s://example.databases.neo4j.io"
    assert settings.neo4j.user == "neo4j-user"
    assert settings.neo4j.password == "secret"
    assert settings.neo4j.database == "aura-db"
    assert settings.neo4j.is_configured is True


def test_settings_allow_neo4j_without_explicit_database(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.databases.neo4j.io")
    monkeypatch.setenv("NEO4J_USER", "neo4j-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_DATABASE", "")

    settings = Settings(_env_file=None)

    assert settings.neo4j.database is None
    assert settings.neo4j.is_configured is True


def test_settings_load_memory_config_from_env(monkeypatch):
    monkeypatch.setenv("MEMORY_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("MEMORY_EMBEDDING_DIMENSIONS", "3072")
    monkeypatch.setenv("SHARED_HISTORY_THREAD_ID", "shared-thread")
    monkeypatch.setenv("CRON_HISTORY_THREAD_ID", "cron-thread")
    monkeypatch.setenv("MAX_CONVERSATION_HISTORY_LEN", "42")
    monkeypatch.setenv("CRON_MESSAGES_AS_SYSTEM", "false")

    settings = Settings(_env_file=None)

    assert settings.memory.embedding.provider == "openai"
    assert settings.memory.embedding.model == "text-embedding-3-large"
    assert settings.memory.embedding.dimensions == 3072
    assert settings.memory.shared_history_thread_id == "shared-thread"
    assert settings.memory.cron_history_thread_id == "cron-thread"
    assert settings.memory.max_conversation_history_len == 42
    assert settings.memory.cron_messages_as_system is False


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

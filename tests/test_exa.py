import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import ModelRetry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings
from src.tools.exa import EXA_SEARCH_URL, _exa_search, web_search


def _make_settings(monkeypatch, api_key: str | None = "test-key") -> None:
    """Point src.tools.exa.settings at a fresh Settings with given EXA_API_KEY."""
    if api_key is None:
        monkeypatch.delenv("EXA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("EXA_API_KEY", api_key)
    monkeypatch.setattr("src.tools.exa.settings", Settings(_env_file=None))


class _FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient usable as `async with`."""

    def __init__(self, response: httpx.Response, capture: dict):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return SimpleNamespace(post=self._post)

    async def __aexit__(self, *exc):
        return False

    async def _post(self, url, json=None, headers=None):
        self._capture["url"] = url
        self._capture["headers"] = headers
        self._capture["payload"] = json
        # Attach a request so response.raise_for_status() works in tests.
        self._response.request = httpx.Request("POST", url)
        return self._response


def _patch_client(monkeypatch, response: httpx.Response, capture: dict):
    monkeypatch.setattr(
        "src.tools.exa.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(response, capture),
    )


@pytest.mark.asyncio
async def test_web_search_raises_model_retry_when_api_key_missing(monkeypatch):
    _make_settings(monkeypatch, api_key=None)

    with pytest.raises(ModelRetry, match="EXA_API_KEY"):
        await web_search("anything")


@pytest.mark.asyncio
async def test_exa_search_returns_highlights_digest(monkeypatch):
    _make_settings(monkeypatch, api_key="test-key")
    capture: dict = {}

    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "title": "Exa docs",
                    "url": "https://exa.ai/docs",
                    "highlights": ["Exa is a search API", "highlights for agents"],
                }
            ]
        },
    )
    _patch_client(monkeypatch, response, capture)

    digest = await _exa_search("test query", "fast", 5)

    assert capture["url"] == EXA_SEARCH_URL
    assert capture["headers"]["Authorization"] == "Bearer test-key"
    assert capture["payload"]["query"] == "test query"
    assert capture["payload"]["type"] == "fast"
    assert capture["payload"]["numResults"] == 5
    assert capture["payload"]["contents"] == {"highlights": True}
    assert "Exa docs" in digest
    assert "https://exa.ai/docs" in digest
    assert "Exa is a search API" in digest


@pytest.mark.asyncio
async def test_exa_search_no_results_returns_empty_message(monkeypatch):
    _make_settings(monkeypatch, api_key="test-key")
    _patch_client(monkeypatch, httpx.Response(200, json={"results": []}), {})

    digest = await _exa_search("obscure", "auto", 3)
    assert "No web results" in digest


@pytest.mark.asyncio
async def test_exa_search_http_error_raises_model_retry(monkeypatch):
    _make_settings(monkeypatch, api_key="test-key")
    _patch_client(monkeypatch, httpx.Response(500, text="boom"), {})

    with pytest.raises(ModelRetry, match="Web search request failed"):
        await _exa_search("test", "auto", 5)


def test_exa_config_defaults(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.exa.num_results == 5
    assert settings.exa.search_type == "auto"
    assert settings.exa.router_search_type == "fast"
    assert settings.exa.timeout == 15
    assert settings.exa.is_configured is False  # no key in test env


def test_exa_config_env_override(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "abc")
    monkeypatch.setenv("EXA_NUM_RESULTS", "8")
    monkeypatch.setenv("EXA_SEARCH_TYPE", "instant")
    monkeypatch.setenv("EXA_ROUTER_SEARCH_TYPE", "instant")
    settings = Settings(_env_file=None)
    assert settings.exa.api_key == "abc"
    assert settings.exa.num_results == 8
    assert settings.exa.search_type == "instant"
    assert settings.exa.router_search_type == "instant"
    assert settings.exa.is_configured is True

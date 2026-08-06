import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.deps import AgentDeps
from src.agent.router import RouteDecision, RouterAgent, RoutedResult
from src.config import Settings


def test_route_decision_validates_actions():
    assert RouteDecision(action="answer", reply="hi").action == "answer"
    assert RouteDecision(action="escalate", reply="").reply == ""


def test_route_decision_rejects_unknown_action():
    with pytest.raises(Exception):
        RouteDecision(action="maybe", reply="x")


def test_router_config_defaults():
    settings = Settings(_env_file=None)
    assert settings.router.enabled is True
    assert settings.router.retries == 0  # fail-fast into escalation
    assert settings.router.request_limit == 3
    assert settings.router.history_window == 6


def test_router_model_default_is_flash_lite():
    settings = Settings(_env_file=None)
    assert settings.router_model == "google-gla:gemini-3.5-flash-lite"
    assert settings.smart_model == "google-gla:gemini-3.6-flash"
    assert settings.fast_model == "google-gla:gemini-3.5-flash-lite"


def test_router_model_env_override(monkeypatch):
    monkeypatch.setenv("ROUTER_MODEL", "google-gla:gemini-3.6-flash")
    settings = Settings(_env_file=None)
    assert settings.router_model == "google-gla:gemini-3.6-flash"


@pytest.mark.asyncio
async def test_router_route_escalates_on_internal_exception(monkeypatch):
    """Any router run failure must collapse to an 'escalate' decision (fail-fast)."""
    router = RouterAgent.__new__(RouterAgent)
    router.agent = SimpleNamespace(
        run=_raise_runtime_error,
    )

    monkeypatch.setattr("src.agent.router.settings.router.history_window", 6)

    deps = AgentDeps(chat_id="c", channel="cli", history_thread_id="main", sender_id="s")
    decision, usage = await router.route("hello", [], deps)

    assert decision.action == "escalate"
    assert decision.reply == ""
    assert usage is None


@pytest.mark.asyncio
async def test_routed_result_exposes_output_and_usage():
    sentinel_usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    result = RoutedResult(output="hi", _usage=sentinel_usage)
    assert result.output == "hi"
    assert result.usage() is sentinel_usage


async def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("boom")

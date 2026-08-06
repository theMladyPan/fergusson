import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.deps import AgentDeps
from src.agent.router import ROUTER_INSTRUCTIONS, RouteDecision, RouterAgent, RoutedResult
from src.config import Settings


def test_route_decision_validates_actions():
    assert RouteDecision(action="answer", reply="hi").action == "answer"
    assert RouteDecision(action="clarify", reply="which account?").action == "clarify"
    assert RouteDecision(action="escalate", reply="").reply == ""


def test_route_decision_rejects_unknown_action():
    with pytest.raises(Exception):
        RouteDecision(action="maybe", reply="x")


def test_router_config_defaults():
    settings = Settings(_env_file=None)
    assert settings.router.enabled is True
    assert settings.router.retries == 1  # one retry, then escalate
    assert settings.router.tool_timeout == 25  # accommodates gws network reads
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


def test_router_escalates_speech_tool_requests():
    assert "speech transcription or synthesis" in ROUTER_INSTRUCTIONS


def test_extract_tool_returns_collects_tool_results():
    from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
    from src.agent.router import _extract_tool_returns

    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="list_inbox_emails", content="Inbox: ...", tool_call_id="t1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="web_search", content={"x": 1}, tool_call_id="t2")]),
    ]
    out = _extract_tool_returns(messages)
    assert "[list_inbox_emails] Inbox: ..." in out
    assert "[web_search]" in out


def test_extract_tool_returns_none_when_no_tools():
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from src.agent.router import _extract_tool_returns

    messages = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    assert _extract_tool_returns(messages) is None


def test_summary_model_default_and_reasoning_effort():
    from src.config import Settings
    s = Settings(_env_file=None)
    assert s.summary_model == "openrouter:openai/gpt-oss-20b:nitro"
    assert s.summary_reasoning_effort == "low"


def test_router_prompt_requires_clarification_for_vague_requests():
    assert 'action="clarify"' in ROUTER_INSTRUCTIONS
    assert "CLARIFY" in ROUTER_INSTRUCTIONS
    # The three worked examples must be present verbatim to anchor behavior.
    assert "Jakub Rubint" in ROUTER_INSTRUCTIONS
    assert "themladypan@gmail.com" in ROUTER_INSTRUCTIONS
    assert "pošli mu správu" in ROUTER_INSTRUCTIONS
    assert "saves the expensive" in ROUTER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_router_route_escalates_on_internal_exception(monkeypatch):
    """Any router run failure must collapse to an 'escalate' decision (fail-fast)."""
    router = RouterAgent.__new__(RouterAgent)
    router.agent = SimpleNamespace(
        run=_raise_runtime_error,
    )

    monkeypatch.setattr("src.agent.router.settings.router.history_window", 6)

    deps = AgentDeps(chat_id="c", channel="cli", history_thread_id="main", sender_id="s")
    decision, usage, router_context = await router.route("hello", [], deps)

    assert decision.action == "escalate"
    assert decision.reply == ""
    assert usage is None
    assert router_context is None


@pytest.mark.asyncio
async def test_routed_result_exposes_output_and_usage():
    sentinel_usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    result = RoutedResult(output="hi", _usage=sentinel_usage)
    assert result.output == "hi"
    assert result.usage() is sentinel_usage


async def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("boom")

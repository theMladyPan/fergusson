import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import RelationalMemoryCapability, _normalize_predicate  # noqa: E402


class _FakeStore:
    def __init__(self, context_response="No relevant memory found."):
        self.context_response = context_response
        self.search_calls = []
        self.fact_calls = []
        self.preference_calls = []
        self.entity_calls = []

    async def ensure_available(self):
        return True

    async def search_memory(self, query: str, memory_types=None, limit: int = 8):
        self.search_calls.append((query, memory_types, limit))
        return self.context_response

    async def store_fact(self, **kwargs):
        self.fact_calls.append(kwargs)
        return "Stored fact."

    async def store_preference(self, **kwargs):
        self.preference_calls.append(kwargs)
        return "Stored preference."

    async def store_entity(self, **kwargs):
        self.entity_calls.append(kwargs)
        return "Stored entity."


@pytest.mark.asyncio
async def test_before_model_request_injects_graph_memory_context():
    store = _FakeStore(context_response="### Preferences\n- [communication] Prefers concise responses")
    capability = RelationalMemoryCapability(store=store)
    request_context = SimpleNamespace(
        messages=[
            ModelRequest(parts=[UserPromptPart(content="Remember that I prefer concise answers.")]),
            ModelRequest(parts=[UserPromptPart(content="How should you answer me?")]),
        ]
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="discord", chat_id="chat-1", sender_id="user-1"))

    result = await capability.before_model_request(ctx, request_context)

    assert len(result.messages) == 3
    injected_part = result.messages[-2].parts[0]
    assert isinstance(injected_part, SystemPromptPart)
    assert "# Graph Memory Context" in injected_part.content
    assert store.search_calls == [("How should you answer me?", None, 6)]


def test_capability_instructions_describe_lean_graph_memory_contract():
    capability = RelationalMemoryCapability(store=_FakeStore())
    text = capability.get_instructions()
    assert "search_memory" in text
    assert "store_fact" in text
    assert "store_preference" in text
    assert "store_entity" in text
    assert "SQLite remains the source of conversation history" in text
    assert "`MEMORY.md` should stay sparse" in text
    assert "POLE+O entities" in text


@pytest.mark.asyncio
async def test_capability_registers_lean_toolset_and_calls_store():
    store = _FakeStore()
    capability = RelationalMemoryCapability(store=store)
    toolset = capability.get_toolset()
    ctx = RunContext(
        deps=SimpleNamespace(channel="cli", chat_id="chat-1", sender_id="user-1"),
        model="test-model",
        usage=RunUsage(),
        retries={},
        run_id="run-1",
    )

    tools = await toolset.get_tools(ctx)

    assert set(tools) == {"search_memory", "store_fact", "store_preference", "store_entity"}

    result = await toolset.call_tool(
        "store_entity",
        {"name": "Paulina", "entity_type": "PERSON", "subtype": "INDIVIDUAL", "note": "friend"},
        ctx,
        tools["store_entity"],
    )

    assert result == "Stored entity."
    assert store.entity_calls == [
        {
            "name": "Paulina",
            "entity_type": "PERSON",
            "subtype": "INDIVIDUAL",
            "description": None,
            "confidence": 0.9,
            "source_kind": "user",
            "source_channel": "cli",
            "source_ref": "run-1:cli:chat-1",
            "source_note": "friend",
        }
    ]


@pytest.mark.asyncio
async def test_before_model_request_skips_empty_or_unavailable_results():
    store = _FakeStore(context_response="No relevant memory found.")
    capability = RelationalMemoryCapability(store=store)
    request_context = SimpleNamespace(messages=[ModelRequest(parts=[UserPromptPart(content="hello")])])

    result = await capability.before_model_request(SimpleNamespace(), request_context)

    assert result.messages == request_context.messages


def test_normalize_predicate_collapses_spaces_and_punctuation():
    assert _normalize_predicate("Has Child") == "has_child"
    assert _normalize_predicate("primary-channel!") == "primary_channel"

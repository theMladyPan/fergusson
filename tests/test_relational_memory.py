import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import RelationalMemoryCapability, _normalize_predicate  # noqa: E402


class _FakeStore:
    def __init__(self, context_response="No relevant memory found."):
        self.context_response = context_response
        self.context_calls = []
        self.search_calls = []
        self.fact_calls = []
        self.preference_calls = []
        self.entity_calls = []
        self.relation_calls = []

    async def ensure_available(self):
        return True

    async def get_memory_context(self, query: str, max_items: int = 8):
        self.context_calls.append((query, max_items))
        return self.context_response

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

    async def store_relation(self, **kwargs):
        self.relation_calls.append(kwargs)
        return "Stored relation."


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
    assert store.context_calls == [("How should you answer me?", 6)]


def test_capability_instructions_include_entity_and_relation_tools():
    capability = RelationalMemoryCapability(store=_FakeStore())
    text = capability.get_instructions()
    assert "search_memory" in text
    assert "store_fact" in text
    assert "store_preference" in text
    assert "store_entity" in text
    assert "store_relation" in text
    assert "facts, preferences, entities, and relations" in text
    assert "correction=true" in text
    assert "Duplicate storage is avoided" in text


def test_normalize_predicate_collapses_spaces_and_punctuation():
    assert _normalize_predicate("Has Child") == "has_child"
    assert _normalize_predicate("primary-channel!") == "primary_channel"

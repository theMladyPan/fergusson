import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import (  # noqa: E402
    RelationalMemoryBatch,
    RelationalMemoryCapability,
    RelationalMemoryFactItem,
    RelationalMemoryPreferenceItem,
    _extractor_instructions,
    _normalize_predicate,
)


class _FakeStore:
    def __init__(self, context_response="No relevant memory found."):
        self.context_response = context_response
        self.context_calls = []
        self.search_calls = []
        self.fact_calls = []
        self.preference_calls = []
        self.similar_calls = []

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

    async def find_similar_memory(self, **kwargs):
        self.similar_calls.append(kwargs)
        return "No similar memory found."


class _FakeExtractor:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


@pytest.mark.asyncio
async def test_before_model_request_injects_graph_memory_context():
    store = _FakeStore(context_response="- [communication] Prefers concise responses")
    capability = RelationalMemoryCapability(store=store, extraction_model="test")
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


@pytest.mark.asyncio
async def test_after_run_persists_extracted_facts_and_preferences():
    store = _FakeStore(context_response="- [food] Vegetarian")
    capability = RelationalMemoryCapability(store=store, extraction_model="test")
    capability._extractor = _FakeExtractor(
        RelationalMemoryBatch(
            facts=[
                RelationalMemoryFactItem(
                    subject="user",
                    predicate="preferred_editor",
                    object_value="Neovim",
                    correction=True,
                    confidence=0.95,
                )
            ],
            preferences=[
                RelationalMemoryPreferenceItem(
                    category="communication",
                    preference="Prefers concise responses",
                    confidence=0.9,
                )
            ],
        )
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="cli", chat_id="cli_chat", sender_id="user-1"), run_id="run-1")
    result = SimpleNamespace(
        output="Noted.",
        new_messages=lambda: [ModelRequest(parts=[UserPromptPart(content="I switched to Neovim and prefer concise answers.")])],
    )

    await capability.after_run(ctx, result=result)

    assert len(store.fact_calls) == 1
    assert store.fact_calls[0]["correction"] is True
    assert len(store.preference_calls) == 1
    assert "Existing memory context:" in capability._extractor.prompts[0]


@pytest.mark.asyncio
async def test_after_run_skips_low_confidence_items():
    store = _FakeStore()
    capability = RelationalMemoryCapability(store=store, extraction_model="test")
    capability._extractor = _FakeExtractor(
        RelationalMemoryBatch(
            facts=[RelationalMemoryFactItem(subject="user", predicate="pet", object_value="dog", confidence=0.2)],
            preferences=[RelationalMemoryPreferenceItem(category="food", preference="likes pizza", confidence=0.1)],
        )
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="cli", chat_id="cli_chat", sender_id="user-1"), run_id="run-1")
    result = SimpleNamespace(
        output="ok",
        new_messages=lambda: [ModelRequest(parts=[UserPromptPart(content="small talk")])],
    )

    await capability.after_run(ctx, result=result)

    assert store.fact_calls == []
    assert store.preference_calls == []


def test_capability_instructions_use_new_tool_names():
    capability = RelationalMemoryCapability(store=_FakeStore(), extraction_model="test")
    text = capability.get_instructions()
    assert "search_memory" in text
    assert "store_fact" in text
    assert "store_preference" in text
    assert "correction=true" in text
    assert "tastes, interests, favorites" in text


def test_extractor_instructions_include_dedup_and_similarity_tool():
    text = _extractor_instructions()
    assert "find_similar_memory" in text
    assert "semantic near-duplicates" in text
    assert "correction=true" in text
    assert "always emit `subject=\"user\"`" in text
    assert "music" in text


def test_normalize_predicate_collapses_spaces_and_punctuation():
    assert _normalize_predicate("Has Child") == "has_child"
    assert _normalize_predicate("primary-channel!") == "primary_channel"

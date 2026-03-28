import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import (
    RelationalMemoryBatch,
    RelationalMemoryCapability,
    RelationalMemoryItem,
)


class _FakeStore:
    def __init__(self, search_response="No relevant relational memories found."):
        self.search_response = search_response
        self.search_calls = []
        self.upsert_calls = []
        self.closed = False

    async def ensure_available(self):
        return True

    async def search_memories(self, query: str, limit: int = 8):
        self.search_calls.append((query, limit))
        return self.search_response

    async def upsert_memory(self, **kwargs):
        self.upsert_calls.append(kwargs)
        return "ok"

    async def close(self):
        self.closed = True


class _FakeExtractor:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


@pytest.mark.asyncio
async def test_before_model_request_injects_relational_memory_context():
    store = _FakeStore(
        search_response="- user (person) -> preferred_editor -> helix [confidence: high, source: user/cli]"
    )
    capability = RelationalMemoryCapability(store=store, extraction_model="test")

    request_context = SimpleNamespace(
        messages=[
            ModelRequest(parts=[UserPromptPart(content="Remember that my favorite editor is Helix.")]),
            ModelRequest(parts=[UserPromptPart(content="What editor did I tell you I prefer?")]),
        ]
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="discord", chat_id="chat-1", sender_id="user-1"))

    result = await capability.before_model_request(ctx, request_context)

    assert len(result.messages) == 3
    assert isinstance(result.messages[-2], ModelRequest)
    injected_part = result.messages[-2].parts[0]
    assert isinstance(injected_part, SystemPromptPart)
    assert "# Relational Memory Context" in injected_part.content
    assert "preferred_editor" in injected_part.content
    assert store.search_calls == [("What editor did I tell you I prefer?", 6)]


@pytest.mark.asyncio
async def test_before_model_request_skips_when_no_matches():
    store = _FakeStore()
    capability = RelationalMemoryCapability(store=store, extraction_model="test")

    request_context = SimpleNamespace(messages=[ModelRequest(parts=[UserPromptPart(content="Hello there")])])
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="cli", chat_id="cli", sender_id="user-1"))

    result = await capability.before_model_request(ctx, request_context)

    assert len(result.messages) == 1


@pytest.mark.asyncio
async def test_after_run_persists_extracted_memories_with_provenance():
    store = _FakeStore()
    capability = RelationalMemoryCapability(store=store, extraction_model="test")
    capability._extractor = _FakeExtractor(
        RelationalMemoryBatch(
            memories=[
                RelationalMemoryItem(
                    subject="user",
                    predicate="preferred_editor",
                    object_value="Helix",
                    subject_type="person",
                    object_type="value",
                    confidence="high",
                ),
                RelationalMemoryItem(
                    subject="Rubint",
                    predicate="works_with",
                    object_value="Metrotech",
                    subject_type="organization",
                    object_type="entity",
                    object_entity_type="organization",
                    confidence="medium",
                ),
            ]
        )
    )

    ctx = SimpleNamespace(
        deps=SimpleNamespace(channel="discord", chat_id="discord-42", sender_id="system_cron"),
        run_id="run-123",
    )
    result = SimpleNamespace(
        output="Noted.",
        new_messages=lambda: [ModelRequest(parts=[UserPromptPart(content="Remember that I prefer Helix.")])],
    )

    returned = await capability.after_run(ctx, result=result)

    assert returned is result
    assert len(store.upsert_calls) == 2
    assert store.upsert_calls[0]["subject"] == "user"
    assert store.upsert_calls[0]["predicate"] == "preferred_editor"
    assert store.upsert_calls[0]["source_kind"] == "system"
    assert store.upsert_calls[0]["source_channel"] == "discord"
    assert "run-123:discord:discord-42" == store.upsert_calls[0]["source_ref"]
    assert store.upsert_calls[1]["object_type"] == "entity"

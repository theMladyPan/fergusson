import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import RelationalMemoryCapability, RelationalMemoryStore, _normalize_predicate  # noqa: E402
from src.config import Neo4jConfig  # noqa: E402


class _FakeStore:
    def __init__(self, context_response="No relevant memory found."):
        self.context_response = context_response
        self.search_calls = []
        self.fact_calls = []
        self.preference_calls = []
        self.entity_calls = []
        self.relation_calls = []

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

    async def store_relation(self, **kwargs):
        self.relation_calls.append(kwargs)
        return "Stored relation."


class _RecordingLongTerm:
    def __init__(self):
        self.fact_writes = []
        self.preference_writes = []
        self.entity_writes = []
        self.relationship_writes = []

    async def add_fact(self, **kwargs):
        self.fact_writes.append(kwargs)

    async def add_preference(self, **kwargs):
        self.preference_writes.append(kwargs)

    async def add_entity(self, *args, **kwargs):
        self.entity_writes.append((args, kwargs))
        entity = SimpleNamespace(id=f"id-{args[0]}", display_name=args[0], name=args[0])
        return entity, SimpleNamespace(action="created")

    async def add_relationship(self, source, target, relationship_type, **kwargs):
        self.relationship_writes.append((source, target, relationship_type, kwargs))


def _build_store() -> tuple[RelationalMemoryStore, _RecordingLongTerm]:
    store = RelationalMemoryStore(Neo4jConfig(uri="neo4j://test", user="neo4j", password="secret"))
    store._available = True
    store._verified = True
    long_term = _RecordingLongTerm()
    store._memory_client = SimpleNamespace(long_term=long_term, graph=SimpleNamespace())
    return store, long_term


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
    assert "store_relation" in text
    assert "SQLite remains the source of conversation history" in text
    assert "`MEMORY.md` should stay sparse" in text
    assert "POLE+O entities" in text
    assert "exact matches, semantic candidates, and a fast-model tie-breaker" in text


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

    assert set(tools) == {"search_memory", "store_fact", "store_preference", "store_entity", "store_relation"}

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

    relation_result = await toolset.call_tool(
        "store_relation",
        {"source_name": "Paulina", "relation_type": "KNOWS", "target_name": "Matus", "note": "friend"},
        ctx,
        tools["store_relation"],
    )

    assert relation_result == "Stored relation."
    assert store.relation_calls == [
        {
            "source_name": "Paulina",
            "relation_type": "KNOWS",
            "target_name": "Matus",
            "source_entity_type": None,
            "source_subtype": None,
            "target_entity_type": None,
            "target_subtype": None,
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


@pytest.mark.asyncio
async def test_store_fact_skips_exact_duplicate(monkeypatch):
    store, long_term = _build_store()

    async def exact_duplicate(**kwargs):
        return True

    monkeypatch.setattr(store, "_exact_fact_exists", exact_duplicate)

    result = await store.store_fact(subject="user", predicate="email", object_value="a@example.com")

    assert result == "Skipped memory fact: exact duplicate."
    assert long_term.fact_writes == []


@pytest.mark.asyncio
async def test_store_fact_skips_semantic_duplicate_after_tiebreak(monkeypatch):
    store, long_term = _build_store()

    async def exact_duplicate(**kwargs):
        return False

    async def semantic_candidates(**kwargs):
        return [{"subject": "user", "predicate": "email", "object": "a@example.com", "similarity": 0.96}]

    async def judge_duplicate(**kwargs):
        return "duplicate"

    monkeypatch.setattr(store, "_exact_fact_exists", exact_duplicate)
    monkeypatch.setattr(store, "_semantic_fact_candidates", semantic_candidates)
    monkeypatch.setattr(store, "_judge_duplicate", judge_duplicate)

    result = await store.store_fact(subject="user", predicate="email", object_value="matus@example.com")

    assert result == "Skipped memory fact: semantic duplicate."
    assert long_term.fact_writes == []


@pytest.mark.asyncio
async def test_store_preference_writes_when_tiebreak_says_distinct(monkeypatch):
    store, long_term = _build_store()

    async def exact_duplicate(**kwargs):
        return False

    async def semantic_candidates(**kwargs):
        return [{"category": "communication", "preference": "brief answers", "context": None, "similarity": 0.93}]

    async def judge_duplicate(**kwargs):
        return "distinct"

    monkeypatch.setattr(store, "_exact_preference_exists", exact_duplicate)
    monkeypatch.setattr(store, "_semantic_preference_candidates", semantic_candidates)
    monkeypatch.setattr(store, "_judge_duplicate", judge_duplicate)

    result = await store.store_preference(category="communication", preference="concise answers")

    assert result == "Stored preference."
    assert len(long_term.preference_writes) == 1


@pytest.mark.asyncio
async def test_store_relation_uses_library_api_after_dedup_checks(monkeypatch):
    store, long_term = _build_store()
    source_entity = SimpleNamespace(id="source-1", display_name="Paulina", name="Paulina")
    target_entity = SimpleNamespace(id="target-1", display_name="Matus", name="Matus")
    created = []

    async def add_entity_and_get_result(**kwargs):
        created.append(kwargs["name"])
        if kwargs["name"] == "Paulina":
            return source_entity, SimpleNamespace(action="created")
        return target_entity, SimpleNamespace(action="created")

    async def relation_candidates(**kwargs):
        return [{"target_id": "other-1", "target_name": "Someone Else", "relation_type": "KNOWS", "description": None}]

    async def judge_duplicate(**kwargs):
        return "distinct"

    monkeypatch.setattr(store, "_add_entity_and_get_result", add_entity_and_get_result)
    monkeypatch.setattr(store, "_relation_candidates", relation_candidates)
    monkeypatch.setattr(store, "_judge_duplicate", judge_duplicate)

    result = await store.store_relation(source_name="Paulina", relation_type="knows", target_name="Matus")

    assert result == "Stored relation."
    assert created == ["Paulina", "Matus"]
    assert long_term.relationship_writes == [
        (
            source_entity,
            target_entity,
            "KNOWS",
            {"description": None, "confidence": 0.9},
        )
    ]


def test_normalize_predicate_collapses_spaces_and_punctuation():
    assert _normalize_predicate("Has Child") == "has_child"
    assert _normalize_predicate("primary-channel!") == "primary_channel"

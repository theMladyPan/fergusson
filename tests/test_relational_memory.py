import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.relational_memory import (  # noqa: E402
    RelationalMemoryBatch,
    RelationalMemoryCapability,
    RelationalMemoryItem,
    RelationalMemoryStore,
    _extractor_instructions,
)
from src.config import Neo4jConfig  # noqa: E402


class _FakeStore:
    def __init__(self, search_response="No relevant relational memories found.", similar_response="No similar relational memories found."):
        self.search_response = search_response
        self.similar_response = similar_response
        self.search_calls = []
        self.similar_calls = []
        self.upsert_calls = []

    async def ensure_available(self):
        return True

    async def search_memories(self, query: str, limit: int = 8):
        self.search_calls.append((query, limit))
        return self.search_response

    async def find_similar_memories(self, **kwargs):
        self.similar_calls.append(kwargs)
        return self.similar_response

    async def upsert_memory(self, **kwargs):
        self.upsert_calls.append(kwargs)
        return "ok"


class _FakeExtractor:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        return SimpleNamespace(output=self.output)


class _StatefulFakeDriver:
    def __init__(self):
        self.entities = {}
        self.assertions = {}
        self.subject_assertions = {}
        self.object_links = {}

    async def verify_connectivity(self):
        return None

    async def close(self):
        return None

    async def execute_query(self, query: str, **params):
        if "CREATE CONSTRAINT" in query:
            return [], None, None

        if "MERGE (entity:MemoryEntity {memory_key: $memory_key})" in query:
            entity = self.entities.setdefault(
                params["memory_key"],
                {"entity_id": f"entity-{len(self.entities) + 1}", "created_at": params["timestamp"]},
            )
            entity.update(
                {
                    "canonical_name": params["canonical_name"],
                    "entity_type": params["entity_type"],
                    "aliases": params["aliases"],
                    "updated_at": params["timestamp"],
                }
            )
            return [], None, None

        if "MATCH (:MemoryEntity {memory_key: $subject_key})-[:ASSERTS]->(assertion:MemoryAssertion {fact_key: $fact_key})" in query:
            fact_key = params["fact_key"]
            subject_key = params["subject_key"]
            assertion = self.assertions.get(fact_key)
            if assertion and fact_key in self.subject_assertions.get(subject_key, set()):
                return ([{"assertion_id": assertion["assertion_id"], "status": assertion["status"]}], None, None)
            return [], None, None

        if "MATCH (:MemoryEntity {memory_key: $subject_key})-[:ASSERTS]->(existing:MemoryAssertion {predicate: $predicate, status: 'active'})" in query:
            count = 0
            for fact_key in self.subject_assertions.get(params["subject_key"], set()):
                assertion = self.assertions[fact_key]
                if assertion["predicate"] == params["predicate"] and assertion["status"] == "active" and fact_key != params["fact_key"]:
                    assertion["status"] = "superseded"
                    assertion["superseded_at"] = params["timestamp"]
                    assertion["last_seen_at"] = params["timestamp"]
                    count += 1
            return ([{"superseded_count": count}], None, None)

        if "MERGE (assertion:MemoryAssertion {fact_key: $fact_key})" in query:
            fact_key = params["fact_key"]
            created = fact_key not in self.assertions
            assertion = self.assertions.setdefault(
                fact_key,
                {"assertion_id": params["assertion_id"], "first_seen_at": params["timestamp"]},
            )
            assertion.update(
                {
                    "predicate": params["predicate"],
                    "value_text": params["value_text"],
                    "display_text": params["display_text"],
                    "object_type": params["object_type"],
                    "status": "active",
                    "confidence": params["confidence"],
                    "source_kind": params["source_kind"],
                    "source_channel": params["source_channel"],
                    "source_ref": params["source_ref"],
                    "source_note": params["source_note"],
                    "last_seen_at": params["timestamp"],
                    "superseded_at": None,
                }
            )
            if created:
                assertion["assertion_id"] = params["assertion_id"]
                assertion["first_seen_at"] = params["timestamp"]
            self.subject_assertions.setdefault(params["subject_key"], set()).add(fact_key)
            if params.get("object_key"):
                self.object_links[fact_key] = params["object_key"]
            return ([{"assertion_id": assertion["assertion_id"]}], None, None)

        if "MATCH (subject:MemoryEntity)-[:ASSERTS]->(assertion:MemoryAssertion {status: 'active'})" in query and "target_fact_key" not in params:
            needle = params["needle"]
            tokens = params["tokens"]
            include_user = params["include_user"]
            rows = []
            for subject_key, fact_keys in self.subject_assertions.items():
                subject = self.entities[subject_key]
                for fact_key in fact_keys:
                    assertion = self.assertions[fact_key]
                    if assertion["status"] != "active":
                        continue
                    object_key = self.object_links.get(fact_key)
                    object_entity = self.entities.get(object_key)
                    haystack = [
                        subject["canonical_name"].casefold(),
                        assertion["predicate"].casefold(),
                        assertion["value_text"].casefold(),
                    ]
                    if object_entity:
                        haystack.append(object_entity["canonical_name"].casefold())
                    matched = any(needle in item for item in haystack) or any(token in item for token in tokens for item in haystack)
                    if include_user and subject_key == "person:user":
                        matched = True
                    if matched:
                        rows.append(
                            {
                                "subject": subject["canonical_name"],
                                "subject_type": subject["entity_type"],
                                "predicate": assertion["predicate"],
                                "value_text": assertion["value_text"],
                                "confidence": assertion["confidence"],
                                "source_channel": assertion["source_channel"],
                                "source_kind": assertion["source_kind"],
                                "source_ref": assertion["source_ref"],
                                "last_seen_at": assertion["last_seen_at"],
                                "object_name": object_entity["canonical_name"] if object_entity else None,
                                "object_type": object_entity["entity_type"] if object_entity else None,
                            }
                        )
            rows.sort(key=lambda row: row["last_seen_at"], reverse=True)
            return rows[: params["limit"]], None, None

        if "target_fact_key" in params:
            rows = []
            for fact_key in self.subject_assertions.get(params["subject_key"], set()):
                assertion = self.assertions[fact_key]
                if assertion["status"] != "active":
                    continue
                if assertion["predicate"] != params["predicate"] and fact_key != params["target_fact_key"]:
                    continue
                object_key = self.object_links.get(fact_key)
                object_entity = self.entities.get(object_key)
                rows.append(
                    {
                        "subject": self.entities[params["subject_key"]]["canonical_name"],
                        "predicate": assertion["predicate"],
                        "value_text": assertion["value_text"],
                        "fact_key": fact_key,
                        "confidence": assertion["confidence"],
                        "source_kind": assertion["source_kind"],
                        "source_channel": assertion["source_channel"],
                        "last_seen_at": assertion["last_seen_at"],
                        "object_name": object_entity["canonical_name"] if object_entity else None,
                    }
                )
            rows.sort(key=lambda row: row["last_seen_at"], reverse=True)
            return rows[: params["limit"]], None, None

        raise AssertionError(f"Unhandled query in fake driver:\n{query}")


def _make_store() -> tuple[RelationalMemoryStore, _StatefulFakeDriver]:
    store = RelationalMemoryStore(Neo4jConfig(enabled=True))
    driver = _StatefulFakeDriver()
    store._driver = driver
    store._available = True
    store._verified = True
    return store, driver


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
    injected_part = result.messages[-2].parts[0]
    assert isinstance(injected_part, SystemPromptPart)
    assert "# Relational Memory Context" in injected_part.content
    assert store.search_calls == [("What editor did I tell you I prefer?", 6)]


@pytest.mark.asyncio
async def test_after_run_passes_replace_existing_to_store():
    store = _FakeStore(search_response="- user -> preferred_editor -> helix")
    capability = RelationalMemoryCapability(store=store, extraction_model="test")
    capability._extractor = _FakeExtractor(
        RelationalMemoryBatch(
            memories=[
                RelationalMemoryItem(
                    subject="user",
                    predicate="preferred_editor",
                    object_value="Neovim",
                    subject_type="person",
                    object_type="value",
                    replace_existing=True,
                    confidence="high",
                )
            ]
        )
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(channel="cli", chat_id="cli_chat", sender_id="user-1"), run_id="run-1")
    result = SimpleNamespace(
        output="Noted.",
        new_messages=lambda: [ModelRequest(parts=[UserPromptPart(content="I switched to Neovim.")])],
    )

    await capability.after_run(ctx, result=result)

    assert store.search_calls == [("I switched to Neovim.", 8)]
    assert store.upsert_calls[0]["replace_existing"] is True
    assert "Existing memory context:" in capability._extractor.prompts[0]


@pytest.mark.asyncio
async def test_upsert_memory_dedups_exact_fact():
    store, driver = _make_store()

    first = await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Helix",
        subject_type="person",
        object_type="value",
    )
    second = await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="helix",
        subject_type="person",
        object_type="value",
    )

    assert first.startswith("Stored relational memory")
    assert second.startswith("Relational memory already stored")
    assert len(driver.assertions) == 1


@pytest.mark.asyncio
async def test_upsert_memory_replace_existing_supersedes_prior_values():
    store, driver = _make_store()

    await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Helix",
        subject_type="person",
        object_type="value",
    )
    await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Neovim",
        subject_type="person",
        object_type="value",
        replace_existing=True,
    )

    statuses = {record["value_text"]: record["status"] for record in driver.assertions.values() if record["predicate"] == "preferred_editor"}
    assert statuses["Helix"] == "superseded"
    assert statuses["Neovim"] == "active"


@pytest.mark.asyncio
async def test_upsert_memory_without_replace_keeps_multiple_values_active():
    store, driver = _make_store()

    await store.upsert_memory(
        subject="user",
        predicate="has_child",
        object_value="Leonard",
        subject_type="person",
        object_type="entity",
        object_entity_type="person",
    )
    await store.upsert_memory(
        subject="user",
        predicate="has_child",
        object_value="Paulina",
        subject_type="person",
        object_type="entity",
        object_entity_type="person",
    )

    active_values = sorted(
        record["value_text"]
        for record in driver.assertions.values()
        if record["predicate"] == "has_child" and record["status"] == "active"
    )
    assert active_values == ["Leonard", "Paulina"]


@pytest.mark.asyncio
async def test_find_similar_memories_returns_exact_marker():
    store, _driver = _make_store()
    await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Helix",
        subject_type="person",
        object_type="value",
    )

    text = await store.find_similar_memories(
        subject="user",
        predicate="preferred_editor",
        object_value="helix",
        subject_type="person",
        object_type="value",
    )
    assert "[exact]" in text
    assert "preferred_editor" in text


@pytest.mark.asyncio
async def test_search_memories_excludes_superseded_values():
    store, _driver = _make_store()
    await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Helix",
        subject_type="person",
        object_type="value",
    )
    await store.upsert_memory(
        subject="user",
        predicate="preferred_editor",
        object_value="Neovim",
        subject_type="person",
        object_type="value",
        replace_existing=True,
    )
    text = await store.search_memories("What editor do I prefer?")
    assert "Neovim" in text
    assert "Helix" not in text


def test_extractor_instructions_include_do_dont_and_lookup_tool():
    text = _extractor_instructions()
    assert "find_similar_relational_memory" in text
    assert "DO examples" in text
    assert "DON'T examples" in text
    assert "replace_existing=true" in text


def test_core_prompt_mentions_replace_existing():
    core_prompt = Path("/home/odroid/fergusson/src/prompt/core.md").read_text(encoding="utf-8")
    assert "Search first before writing any durable fact that may already exist in the database." in core_prompt
    assert "If the user corrects a durable fact, call `upsert_relational_memory` with `replace_existing=true`" in core_prompt

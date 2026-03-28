import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import logfire
from neo4j import AsyncDriver, AsyncGraphDatabase
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, SystemPromptPart, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset

from src.config import Neo4jConfig


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _normalize_subject(subject: str, subject_type: str) -> tuple[str, str]:
    normalized = _normalize_text(subject)
    if normalized in {"i", "me", "my", "mine", "myself", "user"}:
        return "user", "person"
    return normalized, (subject_type or "unknown").strip().lower()


def _entity_payload(name: str, entity_type: str) -> dict[str, str | list[str]]:
    normalized_name, normalized_type = _normalize_subject(name, entity_type)
    return {
        "canonical_name": normalized_name,
        "entity_type": normalized_type,
        "memory_key": f"{normalized_type}:{normalized_name}",
        "aliases": [normalized_name],
    }


def _query_tokens(query: str) -> list[str]:
    tokens = []
    seen = set()
    for token in _normalize_text(query).split():
        if len(token) < 3:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 8:
            break
    return tokens


def _extract_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart):
                return part.content.strip()
    return ""


class RelationalMemoryItem(BaseModel):
    subject: str
    predicate: str
    object_value: str
    subject_type: str = "unknown"
    object_type: Literal["value", "entity"] = "value"
    object_entity_type: str = "unknown"
    confidence: Literal["low", "medium", "high"] = "medium"
    source_note: str | None = None


class RelationalMemoryBatch(BaseModel):
    memories: list[RelationalMemoryItem] = Field(default_factory=list)


class RelationalMemoryStore:
    def __init__(self, config: Neo4jConfig):
        self.config = config
        self._driver: AsyncDriver | None = None
        self._available = False
        self._verified = False
        self._lock = asyncio.Lock()
        self._schema_lock = asyncio.Lock()

    async def ensure_available(self) -> bool:
        if self._verified:
            return self._available

        async with self._lock:
            if self._verified:
                return self._available

            if not self.config.is_configured:
                self._available = False
                self._verified = True
                return False

            try:
                self._driver = AsyncGraphDatabase.driver(
                    self.config.uri,
                    auth=(self.config.user, self.config.password),
                )
                await self._driver.verify_connectivity()
                await self._ensure_schema()
                self._available = True
                logfire.info("Neo4j relational memory enabled", database=self.config.database)
            except Exception as exc:
                self._available = False
                logfire.warning(f"Neo4j relational memory disabled: {exc}")
                if self._driver is not None:
                    await self._driver.close()
                    self._driver = None
            finally:
                self._verified = True

        return self._available

    async def _ensure_schema(self) -> None:
        if self._driver is None:
            return

        async with self._schema_lock:
            await self._driver.execute_query(
                """
                CREATE CONSTRAINT memory_entity_key IF NOT EXISTS
                FOR (e:MemoryEntity) REQUIRE e.memory_key IS UNIQUE
                """,
                database_=self.config.database,
            )
            await self._driver.execute_query(
                """
                CREATE CONSTRAINT memory_assertion_id IF NOT EXISTS
                FOR (a:MemoryAssertion) REQUIRE a.assertion_id IS UNIQUE
                """,
                database_=self.config.database,
            )

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
        self._available = False
        self._verified = False

    async def upsert_memory(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        subject_type: str = "unknown",
        object_type: Literal["value", "entity"] = "value",
        object_entity_type: str = "unknown",
        confidence: str = "high",
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._driver is None:
            return "Relational memory is unavailable."

        subject_payload = _entity_payload(subject, subject_type)
        timestamp = _now_iso()
        params = {
            "subject_key": subject_payload["memory_key"],
            "subject_name": subject_payload["canonical_name"],
            "subject_type": subject_payload["entity_type"],
            "subject_aliases": subject_payload["aliases"],
            "predicate": predicate.strip().lower(),
            "value_text": object_value.strip(),
            "assertion_id": str(uuid.uuid4()),
            "status": "active",
            "confidence": confidence,
            "source_kind": source_kind,
            "source_channel": source_channel,
            "source_ref": source_ref,
            "source_note": source_note,
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "superseded_at": timestamp,
        }

        if object_type == "entity":
            object_payload = _entity_payload(object_value, object_entity_type)
            params.update(
                {
                    "object_key": object_payload["memory_key"],
                    "object_name": object_payload["canonical_name"],
                    "object_type": object_payload["entity_type"],
                    "object_aliases": object_payload["aliases"],
                }
            )
            query = """
            MERGE (subject:MemoryEntity {memory_key: $subject_key})
            ON CREATE SET
              subject.entity_id = randomUUID(),
              subject.created_at = $first_seen_at
            SET
              subject.canonical_name = $subject_name,
              subject.entity_type = $subject_type,
              subject.updated_at = $last_seen_at,
              subject.aliases = reduce(acc = [], alias IN coalesce(subject.aliases, []) + $subject_aliases |
                CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
            MERGE (object:MemoryEntity {memory_key: $object_key})
            ON CREATE SET
              object.entity_id = randomUUID(),
              object.created_at = $first_seen_at
            SET
              object.canonical_name = $object_name,
              object.entity_type = $object_type,
              object.updated_at = $last_seen_at,
              object.aliases = reduce(acc = [], alias IN coalesce(object.aliases, []) + $object_aliases |
                CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
            OPTIONAL MATCH (subject)-[:ASSERTS]->(existing:MemoryAssertion {predicate: $predicate, status: 'active'})
            SET
              existing.status = 'superseded',
              existing.superseded_at = $superseded_at,
              existing.last_seen_at = $last_seen_at
            CREATE (assertion:MemoryAssertion {
              assertion_id: $assertion_id,
              predicate: $predicate,
              value_text: $value_text,
              status: $status,
              confidence: $confidence,
              source_kind: $source_kind,
              source_channel: $source_channel,
              source_ref: $source_ref,
              source_note: $source_note,
              first_seen_at: $first_seen_at,
              last_seen_at: $last_seen_at,
              superseded_at: null
            })
            MERGE (subject)-[:ASSERTS]->(assertion)
            MERGE (assertion)-[:OBJECT]->(object)
            """
        else:
            query = """
            MERGE (subject:MemoryEntity {memory_key: $subject_key})
            ON CREATE SET
              subject.entity_id = randomUUID(),
              subject.created_at = $first_seen_at
            SET
              subject.canonical_name = $subject_name,
              subject.entity_type = $subject_type,
              subject.updated_at = $last_seen_at,
              subject.aliases = reduce(acc = [], alias IN coalesce(subject.aliases, []) + $subject_aliases |
                CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
            OPTIONAL MATCH (subject)-[:ASSERTS]->(existing:MemoryAssertion {predicate: $predicate, status: 'active'})
            SET
              existing.status = 'superseded',
              existing.superseded_at = $superseded_at,
              existing.last_seen_at = $last_seen_at
            CREATE (assertion:MemoryAssertion {
              assertion_id: $assertion_id,
              predicate: $predicate,
              value_text: $value_text,
              status: $status,
              confidence: $confidence,
              source_kind: $source_kind,
              source_channel: $source_channel,
              source_ref: $source_ref,
              source_note: $source_note,
              first_seen_at: $first_seen_at,
              last_seen_at: $last_seen_at,
              superseded_at: null
            })
            MERGE (subject)-[:ASSERTS]->(assertion)
            """

        await self._driver.execute_query(query, **params, database_=self.config.database)
        return (
            f"Stored relational memory: {subject_payload['canonical_name']} -> {predicate.strip().lower()} -> "
            f"{object_value.strip()}"
        )

    async def search_memories(self, query: str, limit: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty relational memory query."

        if not await self.ensure_available() or self._driver is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        needle = _normalize_text(query)
        tokens = _query_tokens(query)
        include_user = any(token in {"remember", "told", "tell", "prefer", "preference", "favorite", "favourite"} for token in tokens)

        records, _, _ = await self._driver.execute_query(
            """
            MATCH (subject:MemoryEntity)-[:ASSERTS]->(assertion:MemoryAssertion {status: 'active'})
            OPTIONAL MATCH (assertion)-[:OBJECT]->(object:MemoryEntity)
            WITH subject, assertion, object,
                 [item IN (
                   [toLower(subject.canonical_name), toLower(assertion.predicate), toLower(coalesce(assertion.value_text, '')), toLower(coalesce(object.canonical_name, ''))] +
                   [alias IN coalesce(subject.aliases, []) | toLower(alias)] +
                   [alias IN coalesce(object.aliases, []) | toLower(alias)]
                 ) WHERE item IS NOT NULL] AS haystack
            WHERE
              any(item IN haystack WHERE item CONTAINS $needle) OR
              any(token IN $tokens WHERE any(item IN haystack WHERE item CONTAINS token)) OR
              ($include_user AND subject.memory_key = 'person:user')
            RETURN
              subject.canonical_name AS subject,
              subject.entity_type AS subject_type,
              assertion.predicate AS predicate,
              assertion.value_text AS value_text,
              assertion.confidence AS confidence,
              assertion.source_channel AS source_channel,
              assertion.source_kind AS source_kind,
              assertion.source_ref AS source_ref,
              assertion.last_seen_at AS last_seen_at,
              object.canonical_name AS object_name,
              object.entity_type AS object_type
            ORDER BY assertion.last_seen_at DESC
            LIMIT $limit
            """,
            needle=needle,
            tokens=tokens,
            include_user=include_user,
            limit=limit,
            database_=self.config.database,
        )

        if not records:
            return "No relevant relational memories found."

        lines = []
        for record in records:
            object_text = record["object_name"] or record["value_text"]
            lines.append(
                f"- {record['subject']} ({record['subject_type']}) -> {record['predicate']} -> {object_text} "
                f"[confidence: {record['confidence']}, source: {record['source_kind']}/{record['source_channel']}]"
            )

        return "\n".join(lines)


@dataclass
class RelationalMemoryCapability(AbstractCapability):
    store: RelationalMemoryStore
    extraction_model: object
    _toolset: FunctionToolset = field(init=False, repr=False)
    _extractor: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._toolset = FunctionToolset(id="relational-memory")

        @self._toolset.tool
        async def search_relational_memory(ctx: RunContext, query: str, limit: int = 8) -> str:
            """
            Search durable relational memories stored in Neo4j.
            Use this when the user asks about preferences, identities, relationships, or prior durable facts.
            """

            del ctx
            return await self.store.search_memories(query=query, limit=limit)

        @self._toolset.tool
        async def upsert_relational_memory(
            ctx: RunContext,
            subject: str,
            predicate: str,
            object_value: str,
            subject_type: str = "unknown",
            object_type: Literal["value", "entity"] = "value",
            object_entity_type: str = "unknown",
            confidence: Literal["low", "medium", "high"] = "high",
            source_note: str | None = None,
        ) -> str:
            """
            Store or update one durable relational memory.
            Use subject 'user' for first-person user facts or preferences.
            """

            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.upsert_memory(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                subject_type=subject_type,
                object_type=object_type,
                object_entity_type=object_entity_type,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=source_note,
            )

        self._extractor = Agent(
            model=self.extraction_model,
            output_type=RelationalMemoryBatch,
            name="RelationalMemoryExtractor",
            defer_model_check=True,
            instructions=(
                "Extract only durable relational memories from the provided conversation turn.\n"
                "Use subject 'user' for first-person user preferences, traits, relationships, and stable facts.\n"
                "Prefer concise predicates like preferred_editor, works_with, accounting_root_folder_id, primary_channel.\n"
                "Set object_type to 'entity' only when the object is a person, organization, place, or other named entity.\n"
                "Ignore transient planning, one-off tasks, tool failures, temporary errors, and speculative guesses.\n"
                "Cron or email-derived factual updates may be stored when they are durable.\n"
                "Return an empty memories list if there is nothing durable to store."
            ),
        )

    def get_instructions(self):
        return (
            "You also have relational memory stored in Neo4j.\n"
            "Use `search_relational_memory` when the user asks about durable facts, preferences, people, organizations, or prior relationships.\n"
            "Use `upsert_relational_memory` for explicit user facts and important durable updates when they belong in structured memory.\n"
            "Relational memory complements `MEMORY.md`; do not copy every turn into it."
        )

    def get_toolset(self):
        return self._toolset

    async def before_model_request(
        self,
        ctx: RunContext,
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if not await self.store.ensure_available():
            return request_context

        latest_user_text = _extract_latest_user_text(request_context.messages)
        if not latest_user_text:
            return request_context

        memory_context = await self.store.search_memories(latest_user_text, limit=6)
        if memory_context.startswith("No relevant relational memories") or memory_context.startswith("Relational memory is unavailable"):
            return request_context

        request_context.messages.insert(
            -1,
            ModelRequest(parts=[SystemPromptPart(content=f"# Relational Memory Context\n{memory_context}")]),
        )
        return request_context

    async def after_run(
        self,
        ctx: RunContext,
        *,
        result: AgentRunResult,
    ) -> AgentRunResult:
        if not await self.store.ensure_available():
            return result

        if not isinstance(result.output, str):
            return result

        user_text = _extract_latest_user_text(result.new_messages())
        assistant_text = result.output.strip()
        if not user_text or not assistant_text:
            return result

        try:
            extraction = await self._extractor.run(
                (
                    f"Channel: {ctx.deps.channel}\n"
                    f"Sender ID: {getattr(ctx.deps, 'sender_id', 'unknown')}\n"
                    f"User message:\n{user_text}\n\n"
                    f"Assistant reply:\n{assistant_text}\n"
                )
            )
        except Exception as exc:
            logfire.warning(f"Relational memory extraction failed: {exc}")
            return result

        source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
        source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"

        for memory in extraction.output.memories:
            if memory.confidence == "low":
                continue
            try:
                await self.store.upsert_memory(
                    subject=memory.subject,
                    predicate=memory.predicate,
                    object_value=memory.object_value,
                    subject_type=memory.subject_type,
                    object_type=memory.object_type,
                    object_entity_type=memory.object_entity_type,
                    confidence=memory.confidence,
                    source_kind=source_kind,
                    source_channel=ctx.deps.channel,
                    source_ref=source_ref,
                    source_note=memory.source_note,
                )
            except Exception as exc:
                logfire.warning(f"Failed to persist extracted relational memory: {exc}")

        return result

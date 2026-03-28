import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import logfire
from jinja2 import Template
from neo4j import AsyncDriver, AsyncGraphDatabase
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset

from src.config import Neo4jConfig


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _normalize_subject(subject: str, subject_type: str) -> tuple[str, str]:
    normalized = _normalize_text(subject)
    if normalized in {"i", "me", "my", "mine", "myself", "user"}:
        return "user", "person"
    normalized_type = _normalize_text(subject_type) or "unknown"
    return normalized, normalized_type


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
        if len(token) < 3 or token in seen:
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


def _build_fact_key(subject_key: str, predicate: str, object_type: Literal["value", "entity"], normalized_object: str) -> str:
    return f"{subject_key}|{predicate}|{object_type}|{normalized_object}"


REMEMBER_QUERY_TOKENS = {"remember", "told", "tell", "prefer", "preference", "favorite", "favourite"}


def _extractor_instructions() -> str:
    template_path = Path(__file__).parents[1] / "prompt" / "relational_memory_extractor.md"
    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())
    return template.render(current_date=datetime.now().strftime("%B %d, %Y"))


class RelationalMemoryItem(BaseModel):
    subject: str
    predicate: str
    object_value: str
    subject_type: str = "unknown"
    object_type: Literal["value", "entity"] = "value"
    object_entity_type: str = "unknown"
    confidence: Literal["low", "medium", "high"] = "medium"
    replace_existing: bool = False
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
            database_kwargs = self._database_kwargs()
            await self._driver.execute_query(
                """
                CREATE CONSTRAINT memory_entity_key IF NOT EXISTS
                FOR (e:MemoryEntity) REQUIRE e.memory_key IS UNIQUE
                """,
                **database_kwargs,
            )
            await self._driver.execute_query(
                """
                CREATE CONSTRAINT memory_assertion_id IF NOT EXISTS
                FOR (a:MemoryAssertion) REQUIRE a.assertion_id IS UNIQUE
                """,
                **database_kwargs,
            )
            await self._driver.execute_query(
                """
                CREATE CONSTRAINT memory_assertion_fact_key IF NOT EXISTS
                FOR (a:MemoryAssertion) REQUIRE a.fact_key IS UNIQUE
                """,
                **database_kwargs,
            )

    def _database_kwargs(self) -> dict[str, str]:
        if self.config.database:
            return {"database_": self.config.database}
        return {}

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
        self._available = False
        self._verified = False

    async def _merge_entity(self, *, payload: dict[str, str | list[str]], timestamp: str) -> None:
        if self._driver is None:
            return

        await self._driver.execute_query(
            """
            MERGE (entity:MemoryEntity {memory_key: $memory_key})
            ON CREATE SET
              entity.entity_id = randomUUID(),
              entity.created_at = $timestamp
            SET
              entity.canonical_name = $canonical_name,
              entity.entity_type = $entity_type,
              entity.updated_at = $timestamp,
              entity.aliases = reduce(acc = [], item IN coalesce(entity.aliases, []) + $aliases |
                CASE WHEN item IN acc THEN acc ELSE acc + item END)
            """,
            memory_key=payload["memory_key"],
            canonical_name=payload["canonical_name"],
            entity_type=payload["entity_type"],
            aliases=payload["aliases"],
            timestamp=timestamp,
            **self._database_kwargs(),
        )

    async def _get_exact_assertion(self, *, subject_key: str, fact_key: str) -> dict | None:
        if self._driver is None:
            return None

        records, _, _ = await self._driver.execute_query(
            """
            MATCH (:MemoryEntity {memory_key: $subject_key})-[:ASSERTS]->(assertion:MemoryAssertion {fact_key: $fact_key})
            RETURN assertion.assertion_id AS assertion_id, assertion.status AS status
            LIMIT 1
            """,
            subject_key=subject_key,
            fact_key=fact_key,
            **self._database_kwargs(),
        )
        return records[0] if records else None

    async def _supersede_conflicting_active_assertions(
        self,
        *,
        subject_key: str,
        predicate: str,
        fact_key: str,
        timestamp: str,
    ) -> int:
        if self._driver is None:
            return 0

        records, _, _ = await self._driver.execute_query(
            """
            MATCH (:MemoryEntity {memory_key: $subject_key})-[:ASSERTS]->(existing:MemoryAssertion {predicate: $predicate, status: 'active'})
            WHERE existing.fact_key <> $fact_key
            SET
              existing.status = 'superseded',
              existing.superseded_at = $timestamp,
              existing.last_seen_at = $timestamp
            RETURN count(existing) AS superseded_count
            """,
            subject_key=subject_key,
            predicate=predicate,
            fact_key=fact_key,
            timestamp=timestamp,
            **self._database_kwargs(),
        )
        if not records:
            return 0
        return int(records[0]["superseded_count"] or 0)

    async def _merge_assertion(
        self,
        *,
        subject_key: str,
        predicate: str,
        fact_key: str,
        value_text: str,
        object_type: Literal["value", "entity"],
        confidence: str,
        source_kind: str,
        source_channel: str,
        source_ref: str,
        source_note: str | None,
        timestamp: str,
        object_key: str | None = None,
    ) -> str:
        if self._driver is None:
            raise RuntimeError("Neo4j driver not available")

        if object_type == "entity":
            query = """
            MATCH (subject:MemoryEntity {memory_key: $subject_key})
            MATCH (object:MemoryEntity {memory_key: $object_key})
            MERGE (assertion:MemoryAssertion {fact_key: $fact_key})
            ON CREATE SET
              assertion.assertion_id = $assertion_id,
              assertion.first_seen_at = $timestamp
            SET
              assertion.predicate = $predicate,
              assertion.value_text = $value_text,
              assertion.display_text = $display_text,
              assertion.object_type = $object_type,
              assertion.status = 'active',
              assertion.confidence = $confidence,
              assertion.source_kind = $source_kind,
              assertion.source_channel = $source_channel,
              assertion.source_ref = $source_ref,
              assertion.source_note = $source_note,
              assertion.last_seen_at = $timestamp,
              assertion.superseded_at = null
            MERGE (subject)-[:ASSERTS]->(assertion)
            MERGE (assertion)-[:OBJECT]->(object)
            RETURN assertion.assertion_id AS assertion_id
            """
        else:
            query = """
            MATCH (subject:MemoryEntity {memory_key: $subject_key})
            MERGE (assertion:MemoryAssertion {fact_key: $fact_key})
            ON CREATE SET
              assertion.assertion_id = $assertion_id,
              assertion.first_seen_at = $timestamp
            SET
              assertion.predicate = $predicate,
              assertion.value_text = $value_text,
              assertion.display_text = $display_text,
              assertion.object_type = $object_type,
              assertion.status = 'active',
              assertion.confidence = $confidence,
              assertion.source_kind = $source_kind,
              assertion.source_channel = $source_channel,
              assertion.source_ref = $source_ref,
              assertion.source_note = $source_note,
              assertion.last_seen_at = $timestamp,
              assertion.superseded_at = null
            MERGE (subject)-[:ASSERTS]->(assertion)
            RETURN assertion.assertion_id AS assertion_id
            """

        records, _, _ = await self._driver.execute_query(
            query,
            subject_key=subject_key,
            object_key=object_key,
            predicate=predicate,
            fact_key=fact_key,
            value_text=value_text,
            display_text=f"{predicate} -> {value_text}",
            object_type=object_type,
            assertion_id=str(uuid.uuid4()),
            confidence=confidence,
            source_kind=source_kind,
            source_channel=source_channel,
            source_ref=source_ref,
            source_note=source_note,
            timestamp=timestamp,
            **self._database_kwargs(),
        )
        return str(records[0]["assertion_id"])

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
        replace_existing: bool = False,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._driver is None:
            return "Relational memory is unavailable."

        normalized_predicate = _normalize_text(predicate)
        if not normalized_predicate:
            return "Skipped relational memory: predicate is empty after normalization."

        normalized_object = _normalize_text(object_value)
        if not normalized_object:
            return "Skipped relational memory: object_value is empty after normalization."

        subject_payload = _entity_payload(subject, subject_type)
        object_payload: dict[str, str | list[str]] | None = None
        if object_type == "entity":
            object_payload = _entity_payload(object_value, object_entity_type)

        fact_key = _build_fact_key(
            str(subject_payload["memory_key"]),
            normalized_predicate,
            object_type,
            normalized_object if object_type == "value" else str(object_payload["memory_key"]),
        )
        timestamp = _now_iso()

        with logfire.span(
            "neo4j_upsert_memory",
            subject=subject_payload["canonical_name"],
            subject_type=subject_payload["entity_type"],
            predicate=normalized_predicate,
            object_type=object_type,
            replace_existing=replace_existing,
            source_kind=source_kind,
            source_channel=source_channel,
            fact_key=fact_key,
        ):
            await self._merge_entity(payload=subject_payload, timestamp=timestamp)
            if object_payload is not None:
                await self._merge_entity(payload=object_payload, timestamp=timestamp)

            exact = await self._get_exact_assertion(subject_key=str(subject_payload["memory_key"]), fact_key=fact_key)
            superseded_count = 0
            if replace_existing:
                superseded_count = await self._supersede_conflicting_active_assertions(
                    subject_key=str(subject_payload["memory_key"]),
                    predicate=normalized_predicate,
                    fact_key=fact_key,
                    timestamp=timestamp,
                )

            await self._merge_assertion(
                subject_key=str(subject_payload["memory_key"]),
                predicate=normalized_predicate,
                fact_key=fact_key,
                value_text=object_value.strip(),
                object_type=object_type,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=source_channel,
                source_ref=source_ref,
                source_note=source_note,
                timestamp=timestamp,
                object_key=str(object_payload["memory_key"]) if object_payload is not None else None,
            )

        action = "inserted"
        if exact is not None and exact["status"] == "active":
            action = "touched_existing"
        elif exact is not None:
            action = "reactivated"
        if superseded_count:
            action = "superseded_and_replaced"

        logfire.info(
            "Relational memory write decision",
            action=action,
            predicate=normalized_predicate,
            subject_key=subject_payload["memory_key"],
            fact_key=fact_key,
        )

        if action == "touched_existing":
            return (
                f"Relational memory already stored: {subject_payload['canonical_name']} -> "
                f"{normalized_predicate} -> {object_value.strip()}"
            )
        if action == "reactivated":
            return (
                f"Reactivated relational memory: {subject_payload['canonical_name']} -> "
                f"{normalized_predicate} -> {object_value.strip()}"
            )
        return (
            f"Stored relational memory: {subject_payload['canonical_name']} -> {normalized_predicate} -> "
            f"{object_value.strip()}"
        )

    async def find_similar_memories(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        subject_type: str = "unknown",
        object_type: Literal["value", "entity"] = "value",
        object_entity_type: str = "unknown",
        limit: int = 6,
    ) -> str:
        if not await self.ensure_available() or self._driver is None:
            return "Relational memory is unavailable."

        normalized_predicate = _normalize_text(predicate)
        subject_payload = _entity_payload(subject, subject_type)
        object_payload = (
            _entity_payload(object_value, object_entity_type)
            if object_type == "entity"
            else None
        )
        normalized_object = _normalize_text(object_value)
        target_fact_key = _build_fact_key(
            str(subject_payload["memory_key"]),
            normalized_predicate,
            object_type,
            normalized_object if object_type == "value" else str(object_payload["memory_key"]),
        )

        records, _, _ = await self._driver.execute_query(
            """
            MATCH (subject:MemoryEntity)-[:ASSERTS]->(assertion:MemoryAssertion {status: 'active'})
            OPTIONAL MATCH (assertion)-[:OBJECT]->(object:MemoryEntity)
            WHERE subject.memory_key = $subject_key
              AND (
                assertion.predicate = $predicate OR
                assertion.fact_key = $target_fact_key OR
                ($object_type = 'value' AND toLower(coalesce(assertion.value_text, '')) CONTAINS $object_norm) OR
                ($object_type = 'entity' AND object.memory_key = $object_key)
              )
            RETURN
              subject.canonical_name AS subject,
              assertion.predicate AS predicate,
              assertion.value_text AS value_text,
              assertion.fact_key AS fact_key,
              assertion.confidence AS confidence,
              assertion.source_kind AS source_kind,
              assertion.source_channel AS source_channel,
              assertion.last_seen_at AS last_seen_at,
              object.canonical_name AS object_name
            ORDER BY assertion.last_seen_at DESC
            LIMIT $limit
            """,
            subject_key=subject_payload["memory_key"],
            predicate=normalized_predicate,
            target_fact_key=target_fact_key,
            object_type=object_type,
            object_norm=normalized_object,
            object_key=object_payload["memory_key"] if object_payload is not None else "",
            limit=limit,
            **self._database_kwargs(),
        )

        if not records:
            return "No similar relational memories found."

        lines = []
        for record in records:
            object_text = record["object_name"] or record["value_text"]
            marker = "exact" if record["fact_key"] == target_fact_key else "related"
            lines.append(
                f"- [{marker}] {record['subject']} -> {record['predicate']} -> {object_text} "
                f"[confidence: {record['confidence']}, source: {record['source_kind']}/{record['source_channel']}]"
            )
        return "\n".join(lines)

    async def search_memories(self, query: str, limit: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty relational memory query."

        if not await self.ensure_available() or self._driver is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        needle = _normalize_text(query)
        tokens = _query_tokens(query)
        include_user = any(token in REMEMBER_QUERY_TOKENS for token in tokens)

        records, _, _ = await self._driver.execute_query(
            """
            MATCH (subject:MemoryEntity)-[:ASSERTS]->(assertion:MemoryAssertion {status: 'active'})
            OPTIONAL MATCH (assertion)-[:OBJECT]->(object:MemoryEntity)
            WITH DISTINCT subject, assertion, object,
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
            **self._database_kwargs(),
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
    _extractor_toolset: FunctionToolset = field(init=False, repr=False)
    _extractor: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._toolset = FunctionToolset(id="relational-memory")
        self._extractor_toolset = FunctionToolset(id="relational-memory-extractor")

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
            replace_existing: bool = False,
            source_note: str | None = None,
        ) -> str:
            """
            Store or update one durable relational memory.
            Use subject 'user' for first-person user facts or preferences.
            Search first and do not store facts that are already present.
            Set replace_existing=true when correcting an existing fact for the same predicate.
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
                replace_existing=replace_existing,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=source_note,
            )

        @self._extractor_toolset.tool
        async def find_similar_relational_memory(
            ctx: RunContext,
            subject: str,
            predicate: str,
            object_value: str,
            subject_type: str = "unknown",
            object_type: Literal["value", "entity"] = "value",
            object_entity_type: str = "unknown",
            limit: int = 6,
        ) -> str:
            """Find similar active memories for dedup/correction decisions before storing new extracted memories."""

            del ctx
            return await self.store.find_similar_memories(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                subject_type=subject_type,
                object_type=object_type,
                object_entity_type=object_entity_type,
                limit=limit,
            )

        self._extractor = Agent(
            model=self.extraction_model,
            output_type=RelationalMemoryBatch,
            name="RelationalMemoryExtractor",
            defer_model_check=True,
            instructions=_extractor_instructions(),
            toolsets=[self._extractor_toolset],
        )

    def get_instructions(self):
        return (
            "You also have relational memory stored in Neo4j.\n"
            "Use `search_relational_memory` when the user asks about durable facts, preferences, people, organizations, or prior relationships.\n"
            "Before calling `upsert_relational_memory`, search first if the fact may already exist.\n"
            "If the same fact is already present, do not store it again.\n"
            "When a user clearly corrects a prior fact for the same predicate, call `upsert_relational_memory` with replace_existing=true.\n"
            "For additive facts, keep replace_existing=false so multiple active facts can coexist.\n"
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

        existing_memory_context = await self.store.search_memories(user_text, limit=8)

        try:
            extraction = await self._extractor.run(
                (
                    f"Channel: {ctx.deps.channel}\n"
                    f"Sender ID: {getattr(ctx.deps, 'sender_id', 'unknown')}\n"
                    f"Existing memory context:\n{existing_memory_context}\n\n"
                    f"User message:\n{user_text}\n\n"
                    f"Assistant reply:\n{assistant_text}\n"
                )
            )
        except Exception as exc:
            logfire.warning("Relational memory extraction failed", error=str(exc))
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
                    replace_existing=memory.replace_existing,
                    source_kind=source_kind,
                    source_channel=ctx.deps.channel,
                    source_ref=source_ref,
                    source_note=memory.source_note,
                )
            except Exception as exc:
                logfire.warning("Failed to persist extracted relational memory", error=str(exc))

        return result

from dataclasses import dataclass, field
from typing import Any

import logfire
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.embeddings import Embedder
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset

from src.config import Neo4jConfig, settings

try:
    from neo4j_agent_memory import (
        EmbeddingConfig,
        EmbeddingProvider,
        ExtractionConfig,
        ExtractorType,
        MemoryClient,
        MemoryConfig,
        MemorySettings,
        Neo4jConfig as NAMNeo4jConfig,
    )
except Exception as _agent_memory_import_exc:  # pragma: no cover - import guard
    MemoryClient = None  # type: ignore[assignment]
    _AGENT_MEMORY_IMPORT_ERROR = str(_agent_memory_import_exc)
else:
    _AGENT_MEMORY_IMPORT_ERROR = None


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _normalize_subject(subject: str) -> str:
    normalized = _normalize_text(subject)
    if normalized in {"i", "me", "my", "mine", "myself", "user"}:
        return "user"
    return normalized


def _normalize_predicate(predicate: str) -> str:
    normalized = _normalize_text(predicate).replace(" ", "_")
    return "".join(char if char.isalnum() or char == "_" else "_" for char in normalized).strip("_")


def _normalize_entity_label(value: str | None, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = _normalize_predicate(value).upper()
    return normalized or default


def _normalize_relation_type(value: str) -> str:
    return _normalize_predicate(value).upper()


def _normalize_optional_text(value: str | None) -> str:
    if value is None:
        return ""
    return _normalize_text(value)


def _entity_display_name(entity: Any) -> str:
    return getattr(entity, "display_name", None) or getattr(entity, "canonical_name", None) or getattr(entity, "name", "unknown")


def _extract_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart):
                return part.content.strip()
    return ""


class PydanticAIEmbedderAdapter:
    """Bridge PydanticAI Embedder to neo4j-agent-memory embedder protocol."""

    def __init__(self, embedder: Embedder, dimensions: int):
        self._embedder = embedder
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> list[float]:
        result = await self._embedder.embed_query(text)
        return result.embeddings[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        result = await self._embedder.embed_documents(texts)
        return result.embeddings


class RelationalMemoryStore:
    def __init__(self, config: Neo4jConfig, fast_model: Any | None = None):
        self.config = config
        self._fast_model = fast_model or settings.fast_model
        self._memory_client: Any | None = None
        self._available = False
        self._verified = False
        self._dedup_agent: Agent | None = None

    async def ensure_available(self) -> bool:
        if self._verified:
            return self._available

        if not self.config.is_configured:
            self._available = False
            self._verified = True
            return False

        if MemoryClient is None:
            self._available = False
            self._verified = True
            logfire.warning("Neo4j relational memory disabled: neo4j-agent-memory not installed", error=_AGENT_MEMORY_IMPORT_ERROR)
            return False

        try:
            self._memory_client = self._build_memory_client()
            await self._memory_client.connect()
            self._available = True
            logfire.info("Neo4j relational memory enabled via neo4j-agent-memory", database=self.config.database)
        except Exception as exc:
            self._available = False
            logfire.warning("Neo4j relational memory disabled", error=str(exc))
            if self._memory_client is not None:
                await self._memory_client.close()
                self._memory_client = None
        finally:
            self._verified = True

        return self._available

    def _build_memory_client(self) -> Any:
        assert self.config.uri is not None
        assert self.config.user is not None
        assert self.config.password is not None

        embedder = Embedder(
            f"{settings.memory.embedding.provider}:{settings.memory.embedding.model}",
            settings={"dimensions": settings.memory.embedding.dimensions},
        )
        embedder_adapter = PydanticAIEmbedderAdapter(embedder, settings.memory.embedding.dimensions)

        memory_settings = MemorySettings(
            neo4j=NAMNeo4jConfig(
                uri=self.config.uri,
                username=self.config.user,
                password=SecretStr(self.config.password),
                database=self.config.database or "neo4j",
            ),
            embedding=EmbeddingConfig(
                provider=EmbeddingProvider.CUSTOM,
                model=f"{settings.memory.embedding.provider}:{settings.memory.embedding.model}",
                dimensions=settings.memory.embedding.dimensions,
            ),
            extraction=ExtractionConfig(extractor_type=ExtractorType.NONE),
            memory=MemoryConfig(fact_deduplication_enabled=True),
        )
        return MemoryClient(memory_settings, embedder=embedder_adapter)

    async def close(self) -> None:
        if self._memory_client is not None:
            await self._memory_client.close()
            self._memory_client = None
        self._available = False
        self._verified = False

    async def _safe_long_term_search(self, label: str, search_coro) -> list[Any]:
        try:
            return await search_coro
        except Exception as exc:
            logfire.warning("Relational memory search failed; skipping section", section=label, error=str(exc))
            return []

    def _get_dedup_agent(self) -> Agent:
        if self._dedup_agent is None:
            self._dedup_agent = Agent(
                self._fast_model,
                name="MemoryDedupJudge",
                system_prompt=(
                    "You decide whether a proposed durable memory duplicates an existing stored memory.\n"
                    "Return exactly one lowercase word: duplicate, distinct, or uncertain.\n"
                    "Use duplicate only when the proposed memory is meaningfully the same durable fact/preference/relation.\n"
                    "Use distinct when both could reasonably coexist as separate memories.\n"
                    "Use uncertain when the evidence is weak or ambiguous.\n"
                    "Do not explain your answer."
                ),
            )
        return self._dedup_agent

    async def _judge_duplicate(
        self,
        *,
        memory_kind: str,
        proposed: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> str:
        if not candidates:
            return "distinct"

        prompt = (
            f"Memory kind: {memory_kind}\n"
            f"Proposed memory:\n{proposed}\n\n"
            f"Candidate existing memories:\n{candidates[:3]}\n\n"
            "Is the proposed memory a duplicate of any candidate?"
        )

        try:
            result = await self._get_dedup_agent().run(prompt)
        except Exception as exc:
            logfire.warning("Memory dedup tie-breaker failed", memory_kind=memory_kind, error=str(exc))
            return "uncertain"

        answer = str(result.output).strip().splitlines()[0].strip().lower()
        if answer in {"duplicate", "distinct", "uncertain"}:
            return answer
        return "uncertain"

    async def _exact_fact_exists(self, *, subject: str, predicate: str, object_value: str) -> bool:
        assert self._memory_client is not None
        facts = await self._memory_client.long_term.get_facts_about(subject, limit=100)
        normalized_object = _normalize_text(object_value)
        for fact in facts:
            if getattr(fact, "valid_until", None) is not None:
                continue
            if _normalize_predicate(fact.predicate) != predicate:
                continue
            if _normalize_text(fact.object) == normalized_object:
                return True
        return False

    async def _semantic_fact_candidates(self, *, subject: str, predicate: str, object_value: str) -> list[dict[str, Any]]:
        assert self._memory_client is not None
        matches = await self._safe_long_term_search(
            "fact_dedup",
            self._memory_client.long_term.search_facts(
                f"{subject} {predicate} {object_value}",
                limit=5,
                threshold=0.9,
            ),
        )
        candidates: list[dict[str, Any]] = []
        for fact in matches:
            if _normalize_subject(fact.subject) != subject:
                continue
            if _normalize_predicate(fact.predicate) != predicate:
                continue
            if getattr(fact, "valid_until", None) is not None:
                continue
            candidates.append(
                {
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "object": fact.object,
                    "similarity": fact.metadata.get("similarity"),
                }
            )
        return candidates

    async def _exact_preference_exists(self, *, category: str, preference: str, context: str | None) -> bool:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_read(
            """
            MATCH (p:Preference)
            WHERE toLower(p.category) = $category
              AND toLower(p.preference) = $preference
              AND toLower(coalesce(p.context, '')) = $context
            RETURN p
            LIMIT 1
            """,
            {
                "category": category.casefold(),
                "preference": _normalize_text(preference),
                "context": _normalize_optional_text(context),
            },
        )
        return bool(results)

    async def _semantic_preference_candidates(
        self,
        *,
        category: str,
        preference: str,
        context: str | None,
    ) -> list[dict[str, Any]]:
        assert self._memory_client is not None
        query = preference if not context else f"{preference} ({context})"
        matches = await self._safe_long_term_search(
            "preference_dedup",
            self._memory_client.long_term.search_preferences(
                query,
                category=category,
                limit=5,
                threshold=0.9,
            ),
        )
        candidates: list[dict[str, Any]] = []
        for pref in matches:
            if _normalize_predicate(pref.category) != category:
                continue
            candidates.append(
                {
                    "category": pref.category,
                    "preference": pref.preference,
                    "context": pref.context,
                    "similarity": pref.metadata.get("similarity"),
                }
            )
        return candidates

    async def _add_entity_and_get_result(
        self,
        *,
        name: str,
        entity_type: str = "OBJECT",
        subtype: str | None = None,
        description: str | None = None,
        confidence: float = 0.9,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> tuple[Any, Any]:
        assert self._memory_client is not None

        display_name = name.strip()
        normalized_type = _normalize_entity_label(entity_type, default="OBJECT") or "OBJECT"
        normalized_subtype = _normalize_entity_label(subtype) if subtype else None
        if _normalize_subject(display_name) == "user":
            display_name = "user"
            normalized_type = "PERSON"
            normalized_subtype = normalized_subtype or "INDIVIDUAL"

        return await self._memory_client.long_term.add_entity(
            display_name,
            normalized_type,
            subtype=normalized_subtype,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
                "source_confidence": confidence,
            },
            resolve=True,
            deduplicate=True,
            enrich=False,
            geocode=False,
        )

    async def _relation_candidates(self, *, source_id: str, relation_type: str) -> list[dict[str, Any]]:
        assert self._memory_client is not None
        rows = await self._memory_client.graph.execute_read(
            """
            MATCH (source:Entity {id: $source_id})-[r:RELATED_TO {type: $relation_type}]->(target:Entity)
            RETURN target, r
            LIMIT 10
            """,
            {"source_id": source_id, "relation_type": relation_type},
        )
        candidates: list[dict[str, Any]] = []
        for row in rows:
            target = dict(row["target"])
            relationship = dict(row["r"]) if hasattr(row["r"], "items") else dict(row["r"]._properties)
            candidates.append(
                {
                    "target_id": target.get("id"),
                    "target_name": target.get("canonical_name") or target.get("name"),
                    "relation_type": relationship.get("type"),
                    "description": relationship.get("description"),
                }
            )
        return candidates

    async def search_memory(self, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty memory query."

        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        include_facts = True
        include_preferences = True
        include_entities = True

        if memory_types:
            normalized = {_normalize_text(item) for item in memory_types if isinstance(item, str) and item.strip()}
            if normalized and normalized.isdisjoint({"all", "long_term", "long-term"}):
                include_facts = bool(normalized & {"fact", "facts"})
                include_preferences = bool(normalized & {"preference", "preferences"})
                include_entities = bool(normalized & {"entity", "entities"})

        parts: list[str] = []

        if include_preferences:
            preferences = await self._safe_long_term_search(
                "preferences",
                self._memory_client.long_term.search_preferences(query, limit=limit),
            )
            if preferences:
                parts.append("### Preferences")
                for pref in preferences[:limit]:
                    line = f"- [{pref.category}] {pref.preference}"
                    if pref.context:
                        line += f" (context: {pref.context})"
                    parts.append(line)

        if include_facts:
            facts = await self._safe_long_term_search(
                "facts",
                self._memory_client.long_term.search_facts(query, limit=limit),
            )
            active_facts = [fact for fact in facts if getattr(fact, "valid_until", None) is None]
            if active_facts:
                if parts:
                    parts.append("")
                parts.append("### Facts")
                for fact in active_facts[:limit]:
                    parts.append(f"- {fact.subject} -> {fact.predicate} -> {fact.object}")

        if include_entities:
            entities = await self._safe_long_term_search(
                "entities",
                self._memory_client.long_term.search_entities(query, limit=limit),
            )
            if entities:
                if parts:
                    parts.append("")
                parts.append("### Entities")
                for entity in entities[:limit]:
                    line = f"- {entity.display_name} ({entity.full_type})"
                    if entity.description:
                        line += f": {entity.description}"
                    parts.append(line)

        return "\n".join(parts).strip() or "No relevant memory found."

    async def store_fact(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        confidence: float = 0.9,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        normalized_subject = _normalize_subject(subject)
        normalized_predicate = _normalize_predicate(predicate)
        object_text = object_value.strip()
        if not normalized_subject or not normalized_predicate or not object_text:
            return "Skipped memory fact: subject, predicate, and object_value are required."

        if await self._exact_fact_exists(
            subject=normalized_subject,
            predicate=normalized_predicate,
            object_value=object_text,
        ):
            return "Skipped memory fact: exact duplicate."

        fact_candidates = await self._semantic_fact_candidates(
            subject=normalized_subject,
            predicate=normalized_predicate,
            object_value=object_text,
        )
        if fact_candidates:
            dedup_decision = await self._judge_duplicate(
                memory_kind="fact",
                proposed={
                    "subject": normalized_subject,
                    "predicate": normalized_predicate,
                    "object": object_text,
                },
                candidates=fact_candidates,
            )
            if dedup_decision == "duplicate":
                return "Skipped memory fact: semantic duplicate."

        await self._memory_client.long_term.add_fact(
            subject=normalized_subject,
            predicate=normalized_predicate,
            obj=object_text,
            confidence=confidence,
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
            },
        )
        return "Stored fact."

    async def store_preference(
        self,
        *,
        category: str,
        preference: str,
        context: str | None = None,
        confidence: float = 0.9,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        normalized_category = _normalize_predicate(category)
        preference_text = preference.strip()
        if not normalized_category or not preference_text:
            return "Skipped preference: category and preference are required."

        normalized_context = context.strip() if isinstance(context, str) and context.strip() else None
        if await self._exact_preference_exists(
            category=normalized_category,
            preference=preference_text,
            context=normalized_context,
        ):
            return "Skipped preference: exact duplicate."

        preference_candidates = await self._semantic_preference_candidates(
            category=normalized_category,
            preference=preference_text,
            context=normalized_context,
        )
        if preference_candidates:
            dedup_decision = await self._judge_duplicate(
                memory_kind="preference",
                proposed={
                    "category": normalized_category,
                    "preference": preference_text,
                    "context": normalized_context,
                },
                candidates=preference_candidates,
            )
            if dedup_decision == "duplicate":
                return "Skipped preference: semantic duplicate."

        await self._memory_client.long_term.add_preference(
            category=normalized_category,
            preference=preference_text,
            context=normalized_context,
            confidence=confidence,
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
            },
        )
        return "Stored preference."

    async def store_entity(
        self,
        *,
        name: str,
        entity_type: str = "OBJECT",
        subtype: str | None = None,
        description: str | None = None,
        confidence: float = 0.9,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        display_name = name.strip()
        if not display_name:
            return "Skipped entity: name is required."

        entity, dedup_result = await self._add_entity_and_get_result(
            name=display_name,
            entity_type=entity_type,
            subtype=subtype,
            description=description,
            confidence=confidence,
            source_kind=source_kind,
            source_channel=source_channel,
            source_ref=source_ref,
            source_note=source_note,
        )
        if getattr(dedup_result, "action", None) == "merged":
            return f"Skipped entity: merged into existing entity {_entity_display_name(entity)}."
        if getattr(dedup_result, "action", None) == "flagged":
            return f"Stored entity and flagged possible duplicate for {_entity_display_name(entity)}."
        return f"Stored entity {_entity_display_name(entity)}."

    async def store_relation(
        self,
        *,
        source_name: str,
        relation_type: str,
        target_name: str,
        source_entity_type: str | None = None,
        source_subtype: str | None = None,
        target_entity_type: str | None = None,
        target_subtype: str | None = None,
        description: str | None = None,
        confidence: float = 0.9,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        normalized_relation_type = _normalize_relation_type(relation_type)
        if not source_name.strip() or not target_name.strip() or not normalized_relation_type:
            return "Skipped relation: source_name, relation_type, and target_name are required."

        source_entity, _ = await self._add_entity_and_get_result(
            name=source_name,
            entity_type=source_entity_type or "OBJECT",
            subtype=source_subtype,
            confidence=confidence,
            source_kind=source_kind,
            source_channel=source_channel,
            source_ref=source_ref,
            source_note=source_note,
        )
        target_entity, _ = await self._add_entity_and_get_result(
            name=target_name,
            entity_type=target_entity_type or "OBJECT",
            subtype=target_subtype,
            confidence=confidence,
            source_kind=source_kind,
            source_channel=source_channel,
            source_ref=source_ref,
            source_note=source_note,
        )

        relation_candidates = await self._relation_candidates(
            source_id=str(source_entity.id),
            relation_type=normalized_relation_type,
        )
        for candidate in relation_candidates:
            if candidate["target_id"] == str(target_entity.id):
                return "Skipped relation: exact duplicate."

        if relation_candidates:
            dedup_decision = await self._judge_duplicate(
                memory_kind="relation",
                proposed={
                    "source_name": _entity_display_name(source_entity),
                    "relation_type": normalized_relation_type,
                    "target_name": _entity_display_name(target_entity),
                    "description": description,
                },
                candidates=relation_candidates,
            )
            if dedup_decision == "duplicate":
                return "Skipped relation: semantic duplicate."

        await self._memory_client.long_term.add_relationship(
            source_entity,
            target_entity,
            normalized_relation_type,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
            confidence=confidence,
        )
        return "Stored relation."


@dataclass
class RelationalMemoryCapability(AbstractCapability):
    store: RelationalMemoryStore
    _toolset: FunctionToolset = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._toolset = FunctionToolset(id="relational-memory")

        @self._toolset.tool
        async def search_memory(ctx: RunContext, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
            """Search graph memory for relevant long-term facts, preferences, and POLE+O entities."""
            del ctx
            return await self.store.search_memory(query=query, memory_types=memory_types, limit=limit)

        @self._toolset.tool
        async def store_fact(
            ctx: RunContext,
            subject: str,
            predicate: str,
            object_value: str,
            confidence: float = 0.9,
            note: str | None = None,
        ) -> str:
            """Store one durable fact in graph memory."""
            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.store_fact(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=note,
            )

        @self._toolset.tool
        async def store_preference(
            ctx: RunContext,
            category: str,
            preference: str,
            context: str | None = None,
            confidence: float = 0.9,
            note: str | None = None,
        ) -> str:
            """Store one durable preference in graph memory."""
            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.store_preference(
                category=category,
                preference=preference,
                context=context,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=note,
            )

        @self._toolset.tool
        async def store_entity(
            ctx: RunContext,
            name: str,
            entity_type: str = "OBJECT",
            subtype: str | None = None,
            description: str | None = None,
            confidence: float = 0.9,
            note: str | None = None,
        ) -> str:
            """Store one durable named entity using the POLE+O model."""
            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.store_entity(
                name=name,
                entity_type=entity_type,
                subtype=subtype,
                description=description,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=note,
            )

        @self._toolset.tool
        async def store_relation(
            ctx: RunContext,
            source_name: str,
            relation_type: str,
            target_name: str,
            source_entity_type: str | None = None,
            source_subtype: str | None = None,
            target_entity_type: str | None = None,
            target_subtype: str | None = None,
            description: str | None = None,
            confidence: float = 0.9,
            note: str | None = None,
        ) -> str:
            """Store one durable relationship between two named entities."""
            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.store_relation(
                source_name=source_name,
                relation_type=relation_type,
                target_name=target_name,
                source_entity_type=source_entity_type,
                source_subtype=source_subtype,
                target_entity_type=target_entity_type,
                target_subtype=target_subtype,
                description=description,
                confidence=confidence,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=note,
            )

    def get_instructions(self):
        return (
            "You also have graph memory stored in Neo4j.\n"
            "`search_memory` can surface durable facts, preferences, and POLE+O entities.\n"
            "Use `store_fact` for scalar durable facts and identifiers.\n"
            "Use `store_preference` for tastes, interests, favorites, and communication style.\n"
            "Use `store_entity` for named people, organizations, places, events, and durable objects.\n"
            "Use `store_relation` when durable meaning depends on a relationship between two entities.\n"
            "Fact, preference, and relation writes check exact matches, semantic candidates, and a fast-model tie-breaker before inserting.\n"
            "SQLite remains the source of conversation history. Neo4j is for durable structured memory only.\n"
            "`MEMORY.md` should stay sparse and hold only the most important anchor objects such as channel IDs, emails, and other critical identifiers."
        )

    def get_toolset(self):
        return self._toolset

    async def before_model_request(
        self,
        ctx: RunContext,
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        del ctx

        if not await self.store.ensure_available():
            return request_context

        latest_user_text = _extract_latest_user_text(request_context.messages)
        if not latest_user_text:
            return request_context

        memory_context = await self.store.search_memory(latest_user_text, limit=6)
        if memory_context.startswith("No relevant memory") or memory_context.startswith("Relational memory is unavailable"):
            return request_context

        request_context.messages.insert(
            -1,
            ModelRequest(parts=[SystemPromptPart(content=f"# Graph Memory Context\n{memory_context}")]),
        )
        return request_context

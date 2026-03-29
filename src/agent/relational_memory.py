from dataclasses import dataclass, field
from typing import Any

import logfire
from pydantic import SecretStr
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
    def __init__(self, config: Neo4jConfig):
        self.config = config
        self._memory_client: Any | None = None
        self._available = False
        self._verified = False

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
            preferences = await self._memory_client.long_term.search_preferences(query, limit=limit)
            if preferences:
                parts.append("### Preferences")
                for pref in preferences[:limit]:
                    line = f"- [{pref.category}] {pref.preference}"
                    if pref.context:
                        line += f" (context: {pref.context})"
                    parts.append(line)

        if include_facts:
            facts = await self._memory_client.long_term.search_facts(query, limit=limit)
            active_facts = [fact for fact in facts if getattr(fact, "valid_until", None) is None]
            if active_facts:
                if parts:
                    parts.append("")
                parts.append("### Facts")
                for fact in active_facts[:limit]:
                    parts.append(f"- {fact.subject} -> {fact.predicate} -> {fact.object}")

        if include_entities:
            entities = await self._memory_client.long_term.search_entities(query, limit=limit)
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

        await self._memory_client.long_term.add_preference(
            category=normalized_category,
            preference=preference_text,
            context=context.strip() if isinstance(context, str) and context.strip() else None,
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

        normalized_type = _normalize_entity_label(entity_type, default="OBJECT") or "OBJECT"
        normalized_subtype = _normalize_entity_label(subtype) if subtype else None
        if _normalize_subject(display_name) == "user":
            display_name = "user"
            normalized_type = "PERSON"
            normalized_subtype = normalized_subtype or "INDIVIDUAL"

        await self._memory_client.long_term.add_entity(
            display_name,
            normalized_type,
            subtype=normalized_subtype,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
            confidence=confidence,
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
            },
            resolve=True,
            deduplicate=True,
            enrich=False,
            geocode=False,
        )
        return f"Stored entity {display_name}."


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

    def get_instructions(self):
        return (
            "You also have graph memory stored in Neo4j.\n"
            "`search_memory` can surface durable facts, preferences, and POLE+O entities.\n"
            "Use `store_fact` for scalar durable facts and identifiers.\n"
            "Use `store_preference` for tastes, interests, favorites, and communication style.\n"
            "Use `store_entity` for named people, organizations, places, events, and durable objects.\n"
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

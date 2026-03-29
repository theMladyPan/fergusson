import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import logfire
from jinja2 import Template
from pydantic import BaseModel, Field
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.embeddings import Embedder
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
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
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart):
                return part.content.strip()
    return ""


def _extractor_instructions() -> str:
    template_path = Path(__file__).parents[1] / "prompt" / "relational_memory_extractor.md"
    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())
    return template.render(current_date=datetime.now().strftime("%B %d, %Y"))


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


class RelationalMemoryFactItem(BaseModel):
    subject: str
    predicate: str
    object_value: str
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    correction: bool = False
    source_note: str | None = None


class RelationalMemoryPreferenceItem(BaseModel):
    category: str
    preference: str
    context: str | None = None
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    source_note: str | None = None


class RelationalMemoryBatch(BaseModel):
    facts: list[RelationalMemoryFactItem] = Field(default_factory=list)
    preferences: list[RelationalMemoryPreferenceItem] = Field(default_factory=list)


class RelationalMemoryStore:
    def __init__(self, config: Neo4jConfig):
        self.config = config
        self._memory_client: Any | None = None
        self._available = False
        self._verified = False
        self._lock = asyncio.Lock()

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
            f"{settings.memory_embedding_provider}:{settings.memory_embedding_model}",
            settings={"dimensions": settings.memory_embedding_dimensions},
        )
        embedder_adapter = PydanticAIEmbedderAdapter(embedder, settings.memory_embedding_dimensions)

        memory_settings = MemorySettings(
            neo4j=NAMNeo4jConfig(
                uri=self.config.uri,
                username=self.config.user,
                password=SecretStr(self.config.password),
                database=self.config.database or "neo4j",
            ),
            embedding=EmbeddingConfig(
                provider=EmbeddingProvider.CUSTOM,
                model=f"{settings.memory_embedding_provider}:{settings.memory_embedding_model}",
                dimensions=settings.memory_embedding_dimensions,
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

    async def _exact_fact_exists(self, *, subject: str, predicate: str, object_value: str) -> bool:
        assert self._memory_client is not None
        facts = await self._memory_client.long_term.get_facts_about(subject, limit=200)
        target = (subject, predicate, _normalize_text(object_value))
        for fact in facts:
            if (
                _normalize_subject(fact.subject),
                _normalize_predicate(fact.predicate),
                _normalize_text(fact.object),
            ) == target:
                return True
        return False

    async def _semantic_fact_exists(self, *, subject: str, predicate: str, object_value: str) -> bool:
        assert self._memory_client is not None
        query = f"{subject} {predicate} {object_value}"
        candidates = await self._memory_client.long_term.search_facts(
            query,
            limit=8,
            threshold=settings.memory_fact_dedup_threshold,
        )
        for candidate in candidates:
            if _normalize_subject(candidate.subject) != subject:
                continue
            if _normalize_predicate(candidate.predicate) != predicate:
                continue
            if _normalize_text(candidate.object) == _normalize_text(object_value):
                return True
            similarity = candidate.metadata.get("similarity")
            if similarity is not None and float(similarity) >= settings.memory_fact_dedup_threshold:
                return True
        return False

    async def _close_conflicting_facts(self, *, subject: str, predicate: str, object_value: str) -> int:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_write(
            """
            MATCH (f:Fact)
            WHERE toLower(f.subject) = $subject
              AND toLower(f.predicate) = $predicate
              AND f.valid_until IS NULL
              AND toLower(f.object) <> $object_value
            SET f.valid_until = datetime($valid_until)
            RETURN count(f) AS closed_count
            """,
            {
                "subject": subject,
                "predicate": predicate,
                "object_value": _normalize_text(object_value),
                "valid_until": _now_iso(),
            },
        )
        return int(results[0].get("closed_count", 0)) if results else 0

    async def store_fact(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        confidence: float = 0.9,
        correction: bool = False,
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
        if await self._semantic_fact_exists(
            subject=normalized_subject,
            predicate=normalized_predicate,
            object_value=object_text,
        ):
            return "Skipped memory fact: semantic duplicate."

        closed_count = 0
        if correction:
            closed_count = await self._close_conflicting_facts(
                subject=normalized_subject,
                predicate=normalized_predicate,
                object_value=object_text,
            )

        await self._memory_client.long_term.add_fact(
            subject=normalized_subject,
            predicate=normalized_predicate,
            obj=object_text,
            confidence=confidence,
            valid_from=datetime.now(UTC),
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
                "correction": correction,
            },
        )
        if correction and closed_count > 0:
            return f"Stored corrected fact and closed {closed_count} conflicting fact(s)."
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
        normalized_preference = _normalize_text(preference)
        if not normalized_category or not normalized_preference:
            return "Skipped preference: category and preference are required."

        existing = await self._memory_client.long_term.search_preferences(
            preference,
            category=normalized_category,
            limit=12,
            threshold=settings.memory_fact_dedup_threshold,
        )
        for pref in existing:
            if _normalize_predicate(pref.category) != normalized_category:
                continue
            if _normalize_text(pref.preference) == normalized_preference:
                return "Skipped preference: exact duplicate."
            similarity = pref.metadata.get("similarity")
            if similarity is not None and float(similarity) >= settings.memory_fact_dedup_threshold:
                return "Skipped preference: semantic duplicate."

        await self._memory_client.long_term.add_preference(
            category=normalized_category,
            preference=preference.strip(),
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

    async def find_similar_memory(self, *, subject: str, predicate: str, object_value: str, limit: int = 6) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        normalized_subject = _normalize_subject(subject)
        normalized_predicate = _normalize_predicate(predicate)
        exact = await self._memory_client.long_term.get_facts_about(normalized_subject, limit=200)
        exact_matches = [
            f for f in exact if _normalize_predicate(f.predicate) == normalized_predicate
        ]
        semantic = await self._memory_client.long_term.search_facts(
            f"{normalized_subject} {normalized_predicate} {object_value}",
            limit=limit,
            threshold=0.6,
        )
        lines: list[str] = []
        for fact in exact_matches[:limit]:
            lines.append(f"- [exact] {fact.subject} -> {fact.predicate} -> {fact.object}")
        for fact in semantic:
            marker = "related"
            if _normalize_text(fact.object) == _normalize_text(object_value):
                marker = "exact"
            similarity = fact.metadata.get("similarity")
            if similarity is None:
                lines.append(f"- [{marker}] {fact.subject} -> {fact.predicate} -> {fact.object}")
            else:
                lines.append(f"- [{marker}] {fact.subject} -> {fact.predicate} -> {fact.object} [similarity: {float(similarity):.2f}]")
        if not lines:
            return "No similar memory found."
        # keep unique lines stable
        return "\n".join(dict.fromkeys(lines))

    async def get_memory_context(self, query: str, max_items: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty memory query."

        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        context = await self._memory_client.get_context(
            query,
            include_short_term=False,
            include_long_term=True,
            include_reasoning=False,
            max_items=max_items,
        )
        return context.strip() or "No relevant memory found."

    async def search_memory(self, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
        del memory_types  # Currently this capability exposes long-term memory only.
        return await self.get_memory_context(query, max_items=limit)


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
        async def search_memory(ctx: RunContext, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
            """Search graph memory for relevant long-term facts and preferences."""
            del ctx
            return await self.store.search_memory(query=query, memory_types=memory_types, limit=limit)

        @self._toolset.tool
        async def get_memory_context(ctx: RunContext, query: str, max_items: int = 8) -> str:
            """Get concise long-term memory context for a query."""
            del ctx
            return await self.store.get_memory_context(query=query, max_items=max_items)

        @self._toolset.tool
        async def store_fact(
            ctx: RunContext,
            subject: str,
            predicate: str,
            object_value: str,
            confidence: float = 0.9,
            correction: bool = False,
            note: str | None = None,
        ) -> str:
            """Store one durable fact. Use correction=true when replacing a previously true value."""
            source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
            source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"
            return await self.store.store_fact(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                confidence=confidence,
                correction=correction,
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
            """Store one durable user preference."""
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

        @self._extractor_toolset.tool
        async def find_similar_memory(
            ctx: RunContext,
            subject: str,
            predicate: str,
            object_value: str,
            limit: int = 6,
        ) -> str:
            """Find exact and semantically similar facts before deciding whether to store a candidate memory."""
            del ctx
            return await self.store.find_similar_memory(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
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
            "You also have graph memory stored in Neo4j.\n"
            "`search_memory` and `get_memory_context` are useful when durable facts and preferences matter.\n"
            "`store_fact` fits durable declarative facts, while `store_preference` fits stable user preferences.\n"
            "For tastes, interests, favorites, and style preferences, prefer `store_preference`.\n"
            "Duplicate storage is avoided: if a fact already exists, skip storing it again.\n"
            "If the user corrects a previously true value for the same subject+predicate, `store_fact` with correction=true can be used.\n"
            "Graph memory complements `MEMORY.md`; concise high-signal anchors can stay in `MEMORY.md` while richer structured detail can live in graph memory."
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

        memory_context = await self.store.get_memory_context(latest_user_text, max_items=6)
        if memory_context.startswith("No relevant memory") or memory_context.startswith("Relational memory is unavailable"):
            return request_context

        request_context.messages.insert(
            -1,
            ModelRequest(parts=[SystemPromptPart(content=f"# Graph Memory Context\n{memory_context}")]),
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

        existing_memory_context = await self.store.get_memory_context(user_text, max_items=8)

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
            logfire.warning("Graph memory extraction failed", error=str(exc))
            return result

        source_kind = "system" if getattr(ctx.deps, "sender_id", None) == "system_cron" else "user"
        source_ref = f"{ctx.run_id}:{ctx.deps.channel}:{ctx.deps.chat_id}"

        for fact in extraction.output.facts:
            if fact.confidence < 0.5:
                continue
            try:
                await self.store.store_fact(
                    subject=fact.subject,
                    predicate=fact.predicate,
                    object_value=fact.object_value,
                    confidence=fact.confidence,
                    correction=fact.correction,
                    source_kind=source_kind,
                    source_channel=ctx.deps.channel,
                    source_ref=source_ref,
                    source_note=fact.source_note,
                )
            except Exception as exc:
                logfire.warning("Failed to persist extracted graph-memory fact", error=str(exc))

        for preference in extraction.output.preferences:
            if preference.confidence < 0.5:
                continue
            try:
                await self.store.store_preference(
                    category=preference.category,
                    preference=preference.preference,
                    context=preference.context,
                    confidence=preference.confidence,
                    source_kind=source_kind,
                    source_channel=ctx.deps.channel,
                    source_ref=source_ref,
                    source_note=preference.source_note,
                )
            except Exception as exc:
                logfire.warning("Failed to persist extracted graph-memory preference", error=str(exc))

        return result

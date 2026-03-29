import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

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
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _normalize_entity_name(name: str) -> str:
    return _normalize_subject(name)


def _normalize_relation_type(value: str) -> str:
    return _normalize_predicate(value).upper()


def _normalize_entity_label(value: str | None, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = _normalize_text(value).replace(" ", "_")
    normalized = re.sub(r"[^a-z0-9_]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        return default
    return normalized.upper()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


_REL_VALID_UNTIL_KEY = "valid_until"


def _serialize_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata)


def _extract_latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart):
                return part.content.strip()
    return ""


def _neo4j_record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    return dict(record)


def _neo4j_rel_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    if hasattr(record, "_properties"):
        return dict(record._properties)
    if hasattr(record, "items"):
        return dict(record)
    return {}


def _entity_display_name(entity: dict[str, Any]) -> str:
    return entity.get("canonical_name") or entity.get("name") or "unknown"


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
            threshold=settings.memory.fact_dedup_threshold,
        )
        for candidate in candidates:
            if _normalize_subject(candidate.subject) != subject:
                continue
            if _normalize_predicate(candidate.predicate) != predicate:
                continue
            if _normalize_text(candidate.object) == _normalize_text(object_value):
                return True
            similarity = candidate.metadata.get("similarity")
            if similarity is not None and float(similarity) >= settings.memory.fact_dedup_threshold:
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

    async def _get_entity_row_by_name(self, *, name: str, entity_type: str | None = None) -> dict[str, Any] | None:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_read(
            """
            MATCH (e:Entity)
            WHERE (
                toLower(e.name) = $name
                OR toLower(coalesce(e.canonical_name, '')) = $name
            )
              AND ($entity_type IS NULL OR toUpper(e.type) = $entity_type)
            RETURN e
            ORDER BY
                CASE
                    WHEN toLower(coalesce(e.canonical_name, '')) = $name THEN 0
                    WHEN toLower(e.name) = $name THEN 1
                    ELSE 2
                END,
                coalesce(e.updated_at, e.created_at) DESC
            LIMIT 1
            """,
            {
                "name": _normalize_entity_name(name),
                "entity_type": entity_type,
            },
        )
        if not results:
            return None
        return _neo4j_record_to_dict(results[0]["e"])

    async def _get_entity_row_by_id(self, entity_id: str) -> dict[str, Any] | None:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_read(
            """
            MATCH (e:Entity {id: $id})
            RETURN e
            LIMIT 1
            """,
            {"id": entity_id},
        )
        if not results:
            return None
        return _neo4j_record_to_dict(results[0]["e"])

    async def _set_entity_confidence(self, *, entity_id: str, confidence: float) -> None:
        assert self._memory_client is not None
        await self._memory_client.graph.execute_write(
            """
            MATCH (e:Entity {id: $id})
            SET e.confidence = $confidence,
                e.updated_at = datetime()
            RETURN e
            """,
            {"id": entity_id, "confidence": confidence},
        )

    async def _ensure_entity(
        self,
        *,
        name: str,
        entity_type: str | None,
        subtype: str | None,
        description: str | None,
        confidence: float,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        assert self._memory_client is not None

        normalized_name = _normalize_entity_name(name)
        if not normalized_name:
            raise ValueError("Entity name is required.")

        if normalized_name == "user":
            stored_name = "user"
            normalized_type = _normalize_entity_label(entity_type, default="PERSON") or "PERSON"
            normalized_subtype = _normalize_entity_label(subtype, default="INDIVIDUAL")
        else:
            stored_name = name.strip()
            normalized_type = _normalize_entity_label(entity_type, default="OBJECT") or "OBJECT"
            normalized_subtype = _normalize_entity_label(subtype)

        existing = await self._get_entity_row_by_name(name=normalized_name, entity_type=normalized_type)
        if existing is not None:
            return existing, "exact"

        entity, dedup_result = await self._memory_client.long_term.add_entity(
            stored_name,
            normalized_type,
            subtype=normalized_subtype,
            description=description.strip() if isinstance(description, str) and description.strip() else None,
            metadata=metadata,
            resolve=True,
            deduplicate=True,
            enrich=False,
            geocode=False,
        )

        entity_id = str(entity.id)
        await self._set_entity_confidence(entity_id=entity_id, confidence=confidence)
        row = await self._get_entity_row_by_id(entity_id)
        if row is None:
            row = {
                "id": entity_id,
                "name": entity.name,
                "canonical_name": entity.canonical_name,
                "type": entity.type,
                "subtype": entity.subtype,
                "description": entity.description,
                "confidence": confidence,
            }

        if dedup_result.action == "merged":
            return row, "merged"
        if dedup_result.action == "flagged":
            return row, "flagged"
        return row, "stored"

    async def _relation_exists(self, *, source_id: str, relation_type: str, target_id: str) -> bool:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_read(
            """
            MATCH (:Entity {id: $source_id})-[r:RELATED_TO {type: $relation_type}]->(:Entity {id: $target_id})
            WHERE r[$valid_until_key] IS NULL
            RETURN count(r) AS relation_count
            """,
            {
                "source_id": source_id,
                "relation_type": relation_type,
                "target_id": target_id,
                "valid_until_key": _REL_VALID_UNTIL_KEY,
            },
        )
        return bool(results and int(results[0].get("relation_count", 0)) > 0)

    async def _close_conflicting_relations(self, *, source_id: str, relation_type: str, target_id: str) -> int:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_write(
            """
            MATCH (:Entity {id: $source_id})-[r:RELATED_TO {type: $relation_type}]->(target:Entity)
            WHERE r[$valid_until_key] IS NULL
              AND target.id <> $target_id
            SET r[$valid_until_key] = datetime($valid_until)
            RETURN count(r) AS closed_count
            """,
            {
                "source_id": source_id,
                "relation_type": relation_type,
                "target_id": target_id,
                "valid_until": _now_iso(),
                "valid_until_key": _REL_VALID_UNTIL_KEY,
            },
        )
        return int(results[0].get("closed_count", 0)) if results else 0

    async def _get_relation_rows_for_entity(self, *, entity_id: str, limit: int) -> list[dict[str, Any]]:
        assert self._memory_client is not None
        results = await self._memory_client.graph.execute_read(
            """
            MATCH (focus:Entity {id: $entity_id})-[r:RELATED_TO]-(other:Entity)
            WITH startNode(r) AS source, r, endNode(r) AS target
            WHERE r[$valid_until_key] IS NULL
            RETURN source, r, target
            ORDER BY coalesce(r.updated_at, r.created_at) DESC
            LIMIT $limit
            """,
            {"entity_id": entity_id, "limit": limit, "valid_until_key": _REL_VALID_UNTIL_KEY},
        )
        rows: list[dict[str, Any]] = []
        for row in results:
            rows.append(
                {
                    "source": _neo4j_record_to_dict(row["source"]),
                    "target": _neo4j_record_to_dict(row["target"]),
                    "relationship": _neo4j_rel_to_dict(row["r"]),
                }
            )
        return rows

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
            threshold=settings.memory.fact_dedup_threshold,
        )
        for pref in existing:
            if _normalize_predicate(pref.category) != normalized_category:
                continue
            if _normalize_text(pref.preference) == normalized_preference:
                return "Skipped preference: exact duplicate."
            similarity = pref.metadata.get("similarity")
            if similarity is not None and float(similarity) >= settings.memory.fact_dedup_threshold:
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

        normalized_name = _normalize_entity_name(name)
        if not normalized_name:
            return "Skipped entity: name is required."

        row, action = await self._ensure_entity(
            name=name,
            entity_type=entity_type,
            subtype=subtype,
            description=description,
            confidence=confidence,
            metadata={
                "source_kind": source_kind,
                "source_channel": source_channel,
                "source_ref": source_ref,
                "source_note": source_note or "",
            },
        )
        display_name = _entity_display_name(row)
        if action == "exact":
            return f"Skipped entity: exact duplicate for {display_name}."
        if action == "merged":
            return f"Skipped entity: merged into existing entity {display_name}."
        if action == "flagged":
            return f"Stored entity and flagged potential duplicate for {display_name}."
        return f"Stored entity {display_name}."

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
        correction: bool = False,
        source_kind: str = "user",
        source_channel: str = "unknown",
        source_ref: str = "",
        source_note: str | None = None,
    ) -> str:
        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable."

        normalized_relation_type = _normalize_relation_type(relation_type)
        if not _normalize_entity_name(source_name) or not normalized_relation_type or not _normalize_entity_name(target_name):
            return "Skipped relation: source_name, relation_type, and target_name are required."

        provenance = {
            "source_kind": source_kind,
            "source_channel": source_channel,
            "source_ref": source_ref,
            "source_note": source_note or "",
            "correction": correction,
        }

        source_row, _ = await self._ensure_entity(
            name=source_name,
            entity_type=source_entity_type,
            subtype=source_subtype,
            description=None,
            confidence=confidence,
            metadata=provenance,
        )
        target_row, _ = await self._ensure_entity(
            name=target_name,
            entity_type=target_entity_type,
            subtype=target_subtype,
            description=None,
            confidence=confidence,
            metadata=provenance,
        )

        source_id = str(source_row["id"])
        target_id = str(target_row["id"])
        if await self._relation_exists(source_id=source_id, relation_type=normalized_relation_type, target_id=target_id):
            return "Skipped relation: exact duplicate."

        closed_count = 0
        if correction:
            closed_count = await self._close_conflicting_relations(
                source_id=source_id,
                relation_type=normalized_relation_type,
                target_id=target_id,
            )

        await self._memory_client.graph.execute_write(
            """
            MATCH (source:Entity {id: $source_id})
            MATCH (target:Entity {id: $target_id})
            CREATE (source)-[r:RELATED_TO {
                id: $id,
                type: $relation_type,
                description: $description,
                confidence: $confidence,
                valid_from: datetime($valid_from),
                metadata: $metadata,
                created_at: datetime()
            }]->(target)
            RETURN r
            """,
            {
                "id": str(uuid4()),
                "source_id": source_id,
                "target_id": target_id,
                "relation_type": normalized_relation_type,
                "description": description.strip() if isinstance(description, str) and description.strip() else None,
                "confidence": confidence,
                "valid_from": _now_iso(),
                "metadata": _serialize_metadata(provenance),
            },
        )

        if correction and closed_count > 0:
            return f"Stored corrected relation and closed {closed_count} conflicting relation(s)."
        return "Stored relation."

    async def _build_memory_context(
        self,
        *,
        query: str,
        include_facts: bool,
        include_preferences: bool,
        include_entities: bool,
        include_relations: bool,
        max_items: int,
    ) -> str:
        assert self._memory_client is not None
        parts: list[str] = []

        if include_preferences:
            preferences = await self._memory_client.long_term.search_preferences(
                query,
                limit=max_items,
                threshold=settings.memory.fact_dedup_threshold,
            )
            if preferences:
                parts.append("### Preferences")
                for pref in preferences[:max_items]:
                    line = f"- [{pref.category}] {pref.preference}"
                    if pref.context:
                        line += f" (context: {pref.context})"
                    parts.append(line)

        if include_facts:
            facts = await self._memory_client.long_term.search_facts(
                query,
                limit=max_items,
                threshold=settings.memory.fact_dedup_threshold,
            )
            active_facts = [fact for fact in facts if getattr(fact, "valid_until", None) is None]
            if active_facts:
                if parts:
                    parts.append("")
                parts.append("### Facts")
                for fact in active_facts[:max_items]:
                    parts.append(f"- {fact.subject} -> {fact.predicate} -> {fact.object}")

        entities: list[Any] = []
        if include_entities or include_relations:
            entities = await self._memory_client.long_term.search_entities(
                query,
                limit=max_items,
                threshold=settings.memory.fact_dedup_threshold,
            )

        if include_entities and entities:
            if parts:
                parts.append("")
            parts.append("### Relevant Entities")
            for entity in entities[:max_items]:
                line = f"- {entity.display_name} ({entity.full_type})"
                if entity.description:
                    line += f": {entity.description}"
                parts.append(line)

        if include_relations and entities:
            relation_lines: list[str] = []
            per_entity_limit = max(1, min(3, max_items))
            for entity in entities[:max_items]:
                for row in await self._get_relation_rows_for_entity(entity_id=str(entity.id), limit=per_entity_limit):
                    source = row["source"]
                    target = row["target"]
                    relationship = row["relationship"]
                    line = f"- {_entity_display_name(source)} -[{relationship.get('type', 'RELATED_TO')}]-> {_entity_display_name(target)}"
                    if relationship.get("description"):
                        line += f": {relationship['description']}"
                    relation_lines.append(line)
                    if len(relation_lines) >= max_items:
                        break
                if len(relation_lines) >= max_items:
                    break
            deduped_relation_lines = list(dict.fromkeys(relation_lines))
            if deduped_relation_lines:
                if parts:
                    parts.append("")
                parts.append("### Relationships")
                parts.extend(deduped_relation_lines[:max_items])

        return "\n".join(parts).strip() or "No relevant memory found."

    async def get_memory_context(self, query: str, max_items: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty memory query."

        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        return await self._build_memory_context(
            query=query,
            include_facts=True,
            include_preferences=True,
            include_entities=True,
            include_relations=True,
            max_items=max_items,
        )

    async def search_memory(self, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
        if not query.strip():
            return "Provide a non-empty memory query."

        if not await self.ensure_available() or self._memory_client is None:
            return "Relational memory is unavailable. Fall back to SQLite history and MEMORY.md."

        include_facts = True
        include_preferences = True
        include_entities = True
        include_relations = True

        if memory_types:
            normalized = {_normalize_text(item) for item in memory_types if isinstance(item, str) and item.strip()}
            if normalized and normalized.isdisjoint({"all", "long_term", "long-term"}):
                include_facts = bool(normalized & {"fact", "facts"})
                include_preferences = bool(normalized & {"preference", "preferences"})
                include_entities = bool(normalized & {"entity", "entities"})
                include_relations = bool(normalized & {"relation", "relations", "relationship", "relationships"})

        return await self._build_memory_context(
            query=query,
            include_facts=include_facts,
            include_preferences=include_preferences,
            include_entities=include_entities,
            include_relations=include_relations,
            max_items=limit,
        )


@dataclass
class RelationalMemoryCapability(AbstractCapability):
    store: RelationalMemoryStore
    _toolset: FunctionToolset = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._toolset = FunctionToolset(id="relational-memory")

        @self._toolset.tool
        async def search_memory(ctx: RunContext, query: str, memory_types: list[str] | None = None, limit: int = 8) -> str:
            """Search graph memory for relevant long-term facts, preferences, entities, and relations."""
            del ctx
            return await self.store.search_memory(query=query, memory_types=memory_types, limit=limit)

        @self._toolset.tool
        async def get_memory_context(ctx: RunContext, query: str, max_items: int = 8) -> str:
            """Get concise long-term graph memory context including facts, preferences, entities, and relations."""
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
            """Store one durable named entity. Use this for people, organizations, places, events, and durable objects."""
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
            correction: bool = False,
            note: str | None = None,
        ) -> str:
            """Store a durable relationship between two entities. Use correction=true when the target replaces a previously true target for the same source and relation type."""
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
                correction=correction,
                source_kind=source_kind,
                source_channel=ctx.deps.channel,
                source_ref=source_ref,
                source_note=note,
            )

    def get_instructions(self):
        return (
            "You also have graph memory stored in Neo4j.\n"
            "`search_memory` and `get_memory_context` can surface durable facts, preferences, entities, and relations.\n"
            "Use `store_fact` for scalar durable facts and identifiers.\n"
            "Use `store_preference` for tastes, interests, favorites, and style preferences.\n"
            "Use `store_entity` for named people, organizations, places, events, and durable objects.\n"
            "Use `store_relation` when durable meaning depends on a connection between two entities.\n"
            "Duplicate storage is avoided across facts, entities, and relations.\n"
            "If the user corrects a previously true value for the same subject+predicate or entity relation, use `correction=true`.\n"
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

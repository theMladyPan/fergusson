# Neo4j Memory (Current)

Fergusson uses `neo4j-agent-memory` as an optional long-term memory layer.

## What Is Stored
- Durable **facts** (`subject`, `predicate`, `object`)
- Durable **preferences** (`category`, `preference`, optional context)
- Durable **entities** (`name`, `type`, optional subtype/description)
- Durable **relations** between entities (`source`, `type`, `target`, optional description/temporal correction)

The feature is additive and does not replace shared SQLite history or `MEMORY.md`.

## Runtime Behavior
- The capability injects relevant graph context before model calls.
- The core agent can call:
  - `search_memory(...)`
  - `get_memory_context(...)`
  - `store_fact(...)`
  - `store_preference(...)`
  - `store_entity(...)`
  - `store_relation(...)`
- Duplicate suppression runs before writes:
  - exact normalized duplicate check
  - semantic similarity check (embedding threshold) for facts and entities
  - exact active-edge duplicate check for relations
- Corrections are temporal:
  - `store_fact(..., correction=true)` closes prior conflicting open facts for the same subject+predicate by setting `valid_until=now`
  - then stores the new corrected fact
  - `store_relation(..., correction=true)` closes prior conflicting open relations for the same source entity + relation type before storing the new target edge
- Retrieval context is assembled locally so injected graph memory can include facts, preferences, entities, and relationships in one block.

## Extractor Policy
- Extractor prompt is template-based (`src/prompt/relational_memory_extractor.md`).
- It must call `find_similar_memory(...)` before emitting each fact candidate.
- It must call `find_similar_entity(...)` before emitting each entity candidate.
- It must call `find_similar_relation(...)` before emitting each relation candidate.
- Prompt rules enforce:
  - do not emit exact duplicates
  - do not emit semantic near-duplicates
  - use `correction=true` only for explicit user corrections on facts or relations

## Embeddings
- Memory embeddings are configured via env:
  - `MEMORY_EMBEDDING_PROVIDER` (default `google-gla`)
  - `MEMORY_EMBEDDING_MODEL` (default `gemini-embedding-001`)
  - `MEMORY_EMBEDDING_DIMENSIONS` (default `1536`)
  - `MEMORY_FACT_DEDUP_THRESHOLD` (default `0.85`)

## Availability
- Neo4j memory is fail-open.
- If Neo4j or the memory library is unavailable, the assistant continues using SQLite history and `MEMORY.md`.

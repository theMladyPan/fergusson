# Neo4j Memory (Current)

Fergusson uses `neo4j-agent-memory` as an optional long-term memory layer.

## What Is Stored
- Durable **facts** (`subject`, `predicate`, `object`)
- Durable **preferences** (`category`, `preference`, optional context)
- Optional related entities handled by the library

The feature is additive and does not replace shared SQLite history or `MEMORY.md`.

## Runtime Behavior
- The capability injects relevant graph context before model calls.
- The core agent can call:
  - `search_memory(...)`
  - `get_memory_context(...)`
  - `store_fact(...)`
  - `store_preference(...)`
- Duplicate suppression runs before writes:
  - exact normalized duplicate check
  - semantic similarity check (embedding threshold)
- Corrections are temporal:
  - `store_fact(..., correction=true)` closes prior conflicting open facts for the same subject+predicate by setting `valid_until=now`
  - then stores the new corrected fact

## Extractor Policy
- Extractor prompt is template-based (`src/prompt/relational_memory_extractor.md`).
- It must call `find_similar_memory(...)` before emitting each fact candidate.
- Prompt rules enforce:
  - do not emit exact duplicates
  - do not emit semantic near-duplicates
  - use `correction=true` only for explicit user corrections

## Embeddings
- Memory embeddings are configured via env:
  - `MEMORY_EMBEDDING_PROVIDER` (default `google-gla`)
  - `MEMORY_EMBEDDING_MODEL` (default `gemini-embedding-001`)
  - `MEMORY_EMBEDDING_DIMENSIONS` (default `1536`)
  - `MEMORY_FACT_DEDUP_THRESHOLD` (default `0.85`)

## Availability
- Neo4j memory is fail-open.
- If Neo4j or the memory library is unavailable, the assistant continues using SQLite history and `MEMORY.md`.

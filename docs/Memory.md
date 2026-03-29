# Neo4j Memory

Fergusson uses `neo4j-agent-memory` as an optional long-term memory layer.

## What Is Stored
- Durable **facts** (`subject`, `predicate`, `object`)
- Durable **preferences** (`category`, `preference`, optional context)
- Durable **entities** (`name`, `type`, optional subtype/description)

The feature is additive and does not replace shared SQLite history. `MEMORY.md` remains a sparse prompt-level anchor sheet for critical identifiers only.

## Runtime Behavior
- The capability injects relevant graph context before model calls.
- Graph-memory writes are explicit via core-agent tool calls only (no automatic post-turn extractor pass).
- The core agent can call:
  - `search_memory(...)`
  - `store_fact(...)`
  - `store_preference(...)`
  - `store_entity(...)`
- Retrieval is intentionally simple: `search_memory(...)` formats matching facts, preferences, and entities into one short plain-text block.
- The repository intentionally does not implement custom relation writes, per-type similarity lookup tools, or temporal correction handling.
- Provenance metadata is still attached to writes (`source_kind`, `source_channel`, `source_ref`, optional note).

## Embeddings
- Memory embeddings are configured via env:
  - `MEMORY_EMBEDDING_PROVIDER` (default `google-gla`)
  - `MEMORY_EMBEDDING_MODEL` (default `gemini-embedding-001`)
  - `MEMORY_EMBEDDING_DIMENSIONS` (default `1536`)

## Availability
- Neo4j memory is fail-open.
- If Neo4j or the memory library is unavailable, the assistant continues using SQLite history and `MEMORY.md`.

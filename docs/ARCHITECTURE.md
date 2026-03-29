# Fergusson Agent Architecture & Skills Strategy

This document outlines the architectural decisions and logic behind the agent system in the Fergusson project. It serves as a guide for how the core agent operates, how shared skills are structured, and how cross-channel communication is handled.

## 1. The Core Agent
The Core Agent (`src/agent/core.py`) is the primary interface for all incoming user requests. It applies native tools and reusable skill instructions directly inside one conversational runtime.

**Logic & Capabilities:**
*   **Intent Recognition:** It analyzes the user's message to determine if it can handle the request directly using its built-in tools (Bash, Filesystem) or if the task requires specialized expertise.
*   **Guardrails:** Given its access to bash execution (`src/tools/bash.py`), it is configured to intercept hazardous commands (like `rm`, `sudo`) and explicitly request user permission before execution.
*   **Memory Integration:** It maintains a persistent context of the conversation using SQLite (`state.db`). It retrieves history from one shared thread across CLI, Discord, and Cron, while preserving transport-specific routing metadata for outbound replies.
*   **Relational Memory Capability:** When `NEO4J_*` env vars are configured, the Core Agent attaches a PydanticAI capability from `src/agent/relational_memory.py`. That capability injects relevant graph-memory context before model requests, exposes explicit relational-memory read/write tools, and auto-extracts durable facts, preferences, entities, and relations after successful turns.
*   **Model Configuration:** The agent now loads `SMART_MODEL` and `FAST_MODEL` directly from environment variables as native PydanticAI `provider:model` strings. Fergusson keeps a thin wrapper only for OpenAI and Google direct-provider strings so existing retry and Logfire instrumentation behavior is preserved.
*   **Loop Protection:** The main conversational run is capped by request count using PydanticAI `UsageLimits(request_limit=10)` by default. This favors fast parallel tool use while stopping excessive guess-and-retry model loops.

## 2. Shared Skills
To keep behavior consistent with Codex-style skills, Fergusson loads skills into the agent prompt instead of creating a separate runtime worker for each skill.

**How it works:**
*   The Core Agent discovers every skill in `workspace/skills/` at startup.
*   Skill metadata is used for an overview table, and the skill instructions are appended to the agent's prompt.
*   When a complex task is identified (e.g., managing a Google Calendar), the agent applies the relevant skill instructions directly while still using the shared toolset.
*   `load_skill_details` now returns only the requested skill. If that skill lists `Required skills`, the agent must decide which prerequisites to load explicitly instead of relying on registry-side recursive bundling.
*   Reusable CLI command patterns should live in shared skills, while domain-specific skills should stay thin and reference those shared skills via `Required skills` metadata instead of duplicating command walkthroughs.

## 3. The Skills Standard
Skills are defined dynamically using the **Claude Code Skills Standard**. They are stored in `workspace/skills/`.

**Structure of a Skill:**
*   **`SKILL.md`:** The primary definition file.
    *   **YAML Frontmatter:** Located between `---` at the top of the file. It must contain at least `name` and `description`. It can also include an optional `tools` list to tell the Core Agent which built-in tools it should stay within while applying that skill. The description is what the agent reads to understand what the skill is capable of.
    *   **Markdown Body:** The instructions for applying the skill. This defines the workflow, rules, and how the agent should approach matching tasks.

**Why this standard?**
Using a file-based standard allows us to hot-swap, update, or add new capabilities to the system without modifying the core python code. The `SkillRegistry` (`src/agent/skills.py`) parses these directories at startup and injects their metadata into the Core Agent's system prompt.
Missing prerequisite skill references are treated as warnings and surfaced back to the agent in the skill catalog/detail output rather than crashing discovery.

## 4. Cross-Channel Awareness
Fergusson operates across multiple channels (CLI, Discord, Cron) via a centralized Redis message broker.

**Architectural Choice:**
*   SQLite short-term memory (`src/agent/memory.py`) uses one canonical shared thread id for all inbound messages. A user can continue the same conversation from CLI, Discord, or Cron without switching context.
*   Transport routing remains channel-specific. Broker messages still carry the source channel and the source `chat_id` needed to reply through Discord or CLI correctly.
*   Cron participates in the same shared history. When configured, cron-originated prompts are stored as `system` entries so they influence future turns as background context rather than ordinary user chat.
*   **Proactive Messaging:** To allow the agent to send messages across boundaries (e.g., asking it in the CLI to ping you on Discord), the Core Agent is equipped with two specific tools:
    1.  `get_recent_chats()`: Queries recent delivery destinations from shared-history metadata to find active channel / `chat_id` pairs.
    2.  `send_message_to_channel(channel, chat_id, message)`: Injects a message directly into the Redis outbound queue for the target channel.

**Implementation Notes:**
*   Shared history configuration lives in `src/config.py` under `settings.memory.shared_history_thread_id`.
*   Model selection also lives in `src/config.py` via env-backed `smart_model` and `fast_model`. Neo4j configuration lives there too via `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and `NEO4J_DATABASE`. `workspace/config/config.json` is limited to non-model runtime config such as channels and MCP servers.
*   The main runtime loop in `src/runners.py` resolves every inbound message to the shared thread before calling the agent and before triggering compaction.
*   Stored rows in `src/db/models.py` continue to record the origin channel, and message metadata stores the original transport `chat_id` used for recent-chat lookup and channel replies.

## 5. Relational Memory Layer
Neo4j adds an optional structured long-term memory layer on top of the shared SQLite thread and `MEMORY.md`.

**Data model:**
*   Relational memory is backed by `neo4j-agent-memory` long-term nodes (`Fact`, `Preference`, `Entity`) plus `RELATED_TO` edges between entities.
*   This repository stores durable facts/preferences/entities via library APIs, stores entity-to-entity relations on the shared graph client, and uses graph metadata for provenance (`source_kind`, `source_channel`, `source_ref`, optional note).

**Behavior:**
*   The capability performs a lightweight memory lookup before model requests and injects a concise `# Graph Memory Context` block only when relevant matches exist.
*   The model can explicitly call `search_memory(...)`, `get_memory_context(...)`, `store_fact(...)`, `store_preference(...)`, `store_entity(...)`, and `store_relation(...)`.
*   Retrieval context is assembled locally so the injected block can include facts, preferences, entities, and relationships instead of only the library's default long-term formatter.
*   Duplicate suppression happens before writes with exact normalized triple checks plus semantic similarity checks for facts, exact + semantic checks for entities, and exact active-edge checks for relations.
*   On corrections (`store_fact(..., correction=true)`), existing open-ended conflicting facts for the same subject+predicate are temporally closed (`valid_until=now`) before writing the new fact.
*   On corrections (`store_relation(..., correction=true)`), existing open-ended conflicting relations for the same source entity + relation type are temporally closed before writing the new target edge.
*   After a successful turn, a separate extractor agent running on the fast model may persist durable inferred facts/preferences/entities/relations. Extraction behavior is guided by a Jinja prompt template under `src/prompt/` with explicit do/don't examples.
*   The extractor calls similarity tools (`find_similar_memory`, `find_similar_entity`, `find_similar_relation`) before emitting each candidate memory.
*   Cron-originated turns can create relational memories when the source content is durable.

**Operational notes:**
*   Neo4j is fail-open. If connectivity verification fails, the assistant keeps working with SQLite history and `MEMORY.md`.
*   The Neo4j driver is initialized lazily and closed from `main.py` during shutdown.

## Migration Note
*   This repository now assumes a fresh or reset SQLite history is acceptable. Existing per-channel rows do not need to be migrated because durable preferences and critical facts belong in `MEMORY.md`.
*   Model/provider aliases are no longer defined in `workspace/config/config.json`. Use native PydanticAI model strings like `openai:...`, `google-gla:...`, or `gateway/...` in `SMART_MODEL` and `FAST_MODEL` instead.
*   Neo4j relational memory is additive. It complements the shared SQLite thread and `MEMORY.md`; it does not replace either one and it does not store full raw conversation history in v1.
*   Existing fact nodes are not backfilled into entities/relations automatically. New turns can now add the richer graph shape directly.

## 6. Future Expansions (Phase 5 & 6)
*   **`MEMORY.md` Scratchpad:** A local file where the agent can write down transient state or plans that survive across reboots.
*   **`ROUTINE.md`:** Defining background tasks that the agent should evaluate periodically without explicit user prompts.

# Fergusson Agent Architecture & Skills Strategy

This document outlines the architectural decisions and logic behind the agent system in the Fergusson project. It serves as a guide for how the core agent operates, how shared skills are structured, and how cross-channel communication is handled.

## 1. The Core Agent (The "Omnipotent" Router)
The Core Agent (`src/agent/core.py`) is the primary interface for all incoming user requests. It acts as an intelligent router and orchestrator.

**Logic & Capabilities:**
*   **Intent Recognition:** It analyzes the user's message to determine if it can handle the request directly using its built-in tools (Bash, Filesystem) or if the task requires specialized expertise.
*   **Guardrails:** Given its access to bash execution (`src/tools/bash.py`), it is configured to intercept hazardous commands (like `rm`, `sudo`) and explicitly request user permission before execution.
*   **Memory Integration:** It maintains a persistent context of the conversation using SQLite (`state.db`). It retrieves history from one shared thread across CLI, Discord, and Cron, while preserving transport-specific routing metadata for outbound replies.
*   **Model Configuration:** The agent now loads `SMART_MODEL` and `FAST_MODEL` directly from environment variables as native PydanticAI `provider:model` strings. Fergusson keeps a thin wrapper only for OpenAI and Google direct-provider strings so existing retry and Logfire instrumentation behavior is preserved.
*   **Loop Protection:** The main conversational run is capped by request count using PydanticAI `UsageLimits(request_limit=10)` by default. This favors fast parallel tool use while stopping excessive guess-and-retry model loops.

## 2. Shared Skills
To keep behavior consistent with Codex-style skills, Fergusson loads skills into the agent prompt instead of creating one sub-agent per skill.

**How it works:**
*   The Core Agent discovers every skill in `workspace/skills/` at startup.
*   Skill metadata is used for an overview table, and the skill instructions are appended to the agent's prompt.
*   When a complex task is identified (e.g., managing a Google Calendar), the agent applies the relevant skill instructions directly while still using the shared toolset.
*   `load_skill_details` now returns only the requested skill. If that skill lists `Required skills`, the agent must decide which prerequisites to load explicitly instead of relying on registry-side recursive bundling.

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
*   Shared history configuration lives in `src/config.py` via `shared_history_thread_id`.
*   Model selection also lives in `src/config.py` via env-backed `smart_model` and `fast_model`. `workspace/config/config.json` is now limited to non-model runtime config such as channels and MCP servers.
*   The main runtime loop in `src/runners.py` resolves every inbound message to the shared thread before calling the agent and before triggering compaction.
*   Stored rows in `src/db/models.py` continue to record the origin channel, and message metadata stores the original transport `chat_id` used for recent-chat lookup and channel replies.

## Migration Note
*   This repository now assumes a fresh or reset SQLite history is acceptable. Existing per-channel rows do not need to be migrated because durable preferences and critical facts belong in `MEMORY.md`.
*   Model/provider aliases are no longer defined in `workspace/config/config.json`. Use native PydanticAI model strings like `openai:...`, `google-gla:...`, or `gateway/...` in `SMART_MODEL` and `FAST_MODEL` instead.

## 5. Future Expansions (Phase 5 & 6)
*   **Graph Memory (Neo4j):** Transitioning from a shared SQLite thread to a semantic graph database to connect concepts, entities, and long-term facts across all conversations.
*   **`MEMORY.md` Scratchpad:** A local file where the agent can write down transient state or plans that survive across reboots.
*   **`ROUTINE.md`:** Defining background tasks that the agent should evaluate periodically without explicit user prompts.
